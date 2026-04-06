#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


ResolveWorkspaceIdFn = Callable[[Any, str | None, str | None], str]
BuildContextFn = Callable[..., dict[str, Any]]
LoadPayloadInputsFn = Callable[..., list[dict[str, Any]]]
ApprovePayloadsFn = Callable[..., dict[str, Any]]
FinalizeTaskStateFn = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class RuntimeMiddlewareDeps:
    resolve_workspace_id: ResolveWorkspaceIdFn
    build_context_package: BuildContextFn
    build_context_debug_package: BuildContextFn
    load_payload_inputs: LoadPayloadInputsFn
    approve_payloads: ApprovePayloadsFn
    finalize_task_state: FinalizeTaskStateFn


def run_recall_middleware(
    config: Any,
    deps: RuntimeMiddlewareDeps,
    *,
    user_id: str,
    workspace_id: str | None,
    cwd: str | None,
    role: str | None,
    query: str,
    trigger: str,
    debug: bool = False,
) -> dict[str, Any]:
    resolved_workspace_id = deps.resolve_workspace_id(config, cwd, workspace_id)
    builder = deps.build_context_debug_package if debug else deps.build_context_package
    return builder(
        config,
        user_id=user_id,
        workspace_id=resolved_workspace_id,
        role=role,
        query=query,
        trigger=trigger,
    )


def load_context_package(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def is_approve_envelope(payload: Any) -> bool:
    return isinstance(payload, dict) and ("context_package" in payload or "payloads" in payload)


def normalize_payload_list(payload: Any, *, source: str) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return payload
    raise RuntimeError(f"{source}: payload input must be a JSON object or array")


def load_approve_input_bundle(
    payload_files: list[str],
    *,
    allow_empty: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, bool]:
    payloads: list[dict[str, Any]] = []
    envelope_context_package: dict[str, Any] | None = None
    envelope_used = False

    def consume(item: Any, *, source: str) -> None:
        nonlocal envelope_context_package, envelope_used
        if is_approve_envelope(item):
            envelope_used = True
            context_package = item.get("context_package")
            if context_package is not None:
                if not isinstance(context_package, dict):
                    raise RuntimeError(f"{source}: context_package must be a JSON object")
                if envelope_context_package is not None:
                    raise RuntimeError(f"{source}: multiple envelope context_package values are not supported")
                envelope_context_package = context_package
            payloads.extend(normalize_payload_list(item.get("payloads", []), source=source))
            return
        payloads.extend(normalize_payload_list(item, source=source))

    if payload_files:
        for path in payload_files:
            item = json.loads(Path(path).read_text(encoding="utf-8"))
            consume(item, source=path)
        return payloads, envelope_context_package, envelope_used

    raw = sys.stdin.read().strip()
    if not raw:
        if allow_empty:
            return [], None, False
        raise RuntimeError("no payload input provided")
    item = json.loads(raw)
    consume(item, source="stdin")
    return payloads, envelope_context_package, envelope_used


def run_approve_middleware(
    config: Any,
    deps: RuntimeMiddlewareDeps,
    *,
    user_id: str,
    workspace_id: str | None,
    cwd: str | None,
    payload_files: list[str],
    context_package_path: str | None,
    writeback: bool,
) -> dict[str, Any]:
    resolved_workspace_id = deps.resolve_workspace_id(config, cwd, workspace_id)
    cli_context_package = load_context_package(context_package_path)
    payloads, envelope_context_package, envelope_used = load_approve_input_bundle(
        payload_files,
        allow_empty=bool(context_package_path),
    )
    resolved_context_package = cli_context_package if cli_context_package is not None else envelope_context_package
    result = deps.approve_payloads(
        config,
        payloads=payloads,
        user_id=user_id,
        workspace_id=resolved_workspace_id,
        writeback=writeback,
        context_package=resolved_context_package,
    )
    result["input_bundle"] = {
        "payload_count": len(payloads),
        "envelope_used": envelope_used,
        "context_package_source": (
            "cli"
            if cli_context_package is not None
            else "envelope"
            if envelope_context_package is not None
            else "none"
        ),
        "envelope_context_package_ignored": cli_context_package is not None and envelope_context_package is not None,
    }
    return result


def run_finalize_middleware(
    config: Any,
    deps: RuntimeMiddlewareDeps,
    *,
    user_id: str,
    workspace_id: str | None,
    cwd: str | None,
    query: str,
) -> dict[str, Any]:
    resolved_workspace_id = deps.resolve_workspace_id(config, cwd, workspace_id)
    return deps.finalize_task_state(
        config,
        user_id=user_id,
        workspace_id=resolved_workspace_id,
        query=query,
    )
