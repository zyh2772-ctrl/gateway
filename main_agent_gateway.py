#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


ROOT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = ROOT_DIR / "main-agent-gateway.toml"
STATE_PATH = ROOT_DIR / "runtime" / "main-agent-gateway-state.json"
VALIDATOR_PATH = ROOT_DIR.parent / "codex-global-multi-agent" / "scripts" / "validate_and_merge.py"


ROLE_ALLOWED_BLOCKS = {
    "Planner": [
        "Relevant Preferences",
        "Workspace Facts",
        "Prior Decisions",
        "Task Continuation State",
        "Known Risks",
    ],
    "Retriever": [
        "Workspace Facts",
        "Retrieved Facts",
    ],
    "Verifier": [
        "Prior Decisions",
        "Known Risks",
        "Retrieved Facts",
    ],
    "Synthesizer": [
        "Relevant Preferences",
        "Workspace Facts",
        "Prior Decisions",
        "Task Continuation State",
        "Known Risks",
        "Retrieved Facts",
    ],
}


@dataclass
class GatewayConfig:
    host: str
    port: int
    upstream_base_url: str
    upstream_health_path: str
    upstream_timeout_seconds: int
    default_user_id: str
    default_workspace_id: str
    default_app: str
    compat_helper: Path
    max_memories: int
    max_stage_context_tokens: int
    max_long_term_context_tokens: int
    writeback_enabled: bool
    store_audit_records: bool


def load_config(path: Path) -> GatewayConfig:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    server = raw["server"]
    upstream = raw["upstream"]
    memory = raw["memory"]
    budget = raw["budget"]
    writeback = raw.get("writeback", {})

    return GatewayConfig(
        host=server.get("host", "127.0.0.1"),
        port=int(os.environ.get("MAIN_AGENT_GATEWAY_PORT", server.get("port", 4011))),
        upstream_base_url=os.environ.get("MAIN_AGENT_UPSTREAM_BASE_URL", upstream["base_url"]).rstrip("/"),
        upstream_health_path=upstream.get("health_path", "/models"),
        upstream_timeout_seconds=int(upstream.get("timeout_seconds", 1800)),
        default_user_id=memory.get("default_user_id", "zyh"),
        default_workspace_id=memory.get("default_workspace_id", "default-workspace"),
        default_app=memory.get("default_app", "openmemory"),
        compat_helper=(path.parent / memory["compat_helper"]).resolve(),
        max_memories=int(memory.get("max_memories", 50)),
        max_stage_context_tokens=int(budget.get("max_stage_context_tokens", 1200)),
        max_long_term_context_tokens=int(budget.get("max_long_term_context_tokens", 1200)),
        writeback_enabled=bool(writeback.get("enabled", True)),
        store_audit_records=bool(writeback.get("store_audit_records", True)),
    )


def ensure_runtime_paths() -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_PATH.exists():
        STATE_PATH.write_text("{}\n", encoding="utf-8")


def load_state() -> dict[str, Any]:
    ensure_runtime_paths()
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(state: dict[str, Any]) -> None:
    ensure_runtime_paths()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_validator() -> Any:
    spec = importlib.util.spec_from_file_location("validate_and_merge", VALIDATOR_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError("failed to load validator module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = load_validator()


def run_compat_command(helper: Path, command: list[str]) -> dict[str, Any] | list[Any]:
    result = subprocess.run(
        ["python3", str(helper), *command],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "compat helper failed")
    text = result.stdout.strip() or "{}"
    return json.loads(text)


def list_memories(config: GatewayConfig, user_id: str) -> list[dict[str, Any]]:
    payload = run_compat_command(config.compat_helper, ["list", "--user", user_id])
    if isinstance(payload, list):
        return payload[: config.max_memories]
    return []


def add_memory(config: GatewayConfig, user_id: str, content: str, metadata: dict[str, Any]) -> dict[str, Any]:
    return run_compat_command(
        config.compat_helper,
        [
            "add",
            "--user",
            user_id,
            "--app",
            config.default_app,
            "--content",
            content,
            "--metadata",
            json.dumps(metadata, ensure_ascii=False),
        ],
    )


def update_memory(config: GatewayConfig, memory_id: str, *, content: str | None = None, state: str | None = None) -> dict[str, Any]:
    command = ["update", "--id", memory_id]
    if content is not None:
        command.extend(["--content", content])
    if state is not None:
        command.extend(["--state", state])
    return run_compat_command(config.compat_helper, command)


def extract_text_fragments(value: Any) -> list[str]:
    texts: list[str] = []
    if isinstance(value, str):
        texts.append(value)
        return texts
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            texts.append(value["text"])
        if isinstance(value.get("content"), list):
            for item in value["content"]:
                texts.extend(extract_text_fragments(item))
        elif value.get("content") is not None:
            texts.extend(extract_text_fragments(value["content"]))
        if isinstance(value.get("input"), list):
            for item in value["input"]:
                texts.extend(extract_text_fragments(item))
        return texts
    if isinstance(value, list):
        for item in value:
            texts.extend(extract_text_fragments(item))
    return texts


def extract_query(payload: dict[str, Any]) -> str:
    candidates: list[str] = []
    for key in ("input", "messages"):
        if key in payload:
            candidates.extend(extract_text_fragments(payload[key]))
    text = " ".join(part.strip() for part in candidates if part and part.strip())
    return text[:1200]


def classify_memory_item(memory: dict[str, Any], workspace_id: str) -> tuple[str | None, str]:
    metadata = memory.get("metadata", {}) or {}
    scope = metadata.get("scope", "task")
    item_workspace = metadata.get("workspace_id")
    if scope == "workspace" and item_workspace not in {None, workspace_id}:
        return None, scope
    if scope == "task" and item_workspace not in {None, workspace_id}:
        return None, scope

    kind = metadata.get("kind")
    if kind == "preferences":
        return "Relevant Preferences", scope
    if kind == "facts" and scope == "workspace":
        return "Workspace Facts", scope
    if kind == "decisions":
        return "Prior Decisions", scope
    if kind == "risks":
        return "Known Risks", scope
    if kind in {"retrieved_facts", "retrieved_fact"}:
        return "Retrieved Facts", scope
    if kind in {"task_summary", "task_state", "fallback_history"} or scope == "task":
        return "Task Continuation State", scope
    return None, scope


def score_memory(memory: dict[str, Any], query: str, role: str | None) -> int:
    metadata = memory.get("metadata", {}) or {}
    score = 0
    if metadata.get("status") == "approved":
        score += 3
    if metadata.get("approved_by"):
        score += 1
    if role and metadata.get("role") == role:
        score += 1
    content = (memory.get("content") or "").lower()
    query_terms = [term for term in query.lower().split() if len(term) > 2][:8]
    score += sum(1 for term in query_terms if term in content)
    return score


def trim_items(items: list[str], limit: int) -> list[str]:
    return items[:limit]


def build_context_blocks(
    config: GatewayConfig,
    *,
    user_id: str,
    workspace_id: str,
    role: str | None,
    query: str,
    trigger: str,
) -> dict[str, list[str]]:
    memories = list_memories(config, user_id)
    blocks: dict[str, list[tuple[int, str]]] = {
        "Relevant Preferences": [],
        "Workspace Facts": [],
        "Prior Decisions": [],
        "Task Continuation State": [],
        "Known Risks": [],
        "Retrieved Facts": [],
    }

    for memory in memories:
        block_name, scope = classify_memory_item(memory, workspace_id)
        if block_name is None:
            continue
        metadata = memory.get("metadata", {}) or {}
        if metadata.get("state") in {"draft", "experimental", "outdated", "superseded"}:
            continue
        if metadata.get("state") in {"archived", "deleted"} or memory.get("state") in {"archived", "deleted"}:
            continue
        if block_name == "Task Continuation State" and trigger == "before_task_start":
            if metadata.get("kind") not in {"task_summary", "task_state", "fallback_history"} and scope == "task":
                continue
        score = score_memory(memory, query=query, role=role)
        blocks[block_name].append((score, memory.get("content", "")))

    rendered: dict[str, list[str]] = {}
    for name, values in blocks.items():
        ordered = [text for _, text in sorted(values, key=lambda item: item[0], reverse=True) if text]
        if name == "Relevant Preferences":
            rendered[name] = trim_items(ordered, 4)
        elif name == "Workspace Facts":
            rendered[name] = trim_items(ordered, 5)
        elif name == "Prior Decisions":
            rendered[name] = trim_items(ordered, 5)
        elif name == "Task Continuation State":
            rendered[name] = trim_items(ordered, 4)
        elif name == "Known Risks":
            rendered[name] = trim_items(ordered, 3)
        else:
            rendered[name] = trim_items(ordered, 6)
    return rendered


def select_role_blocks(blocks: dict[str, list[str]], role: str | None) -> dict[str, list[str]]:
    if role is None or role not in ROLE_ALLOWED_BLOCKS:
        return {name: values for name, values in blocks.items() if values}
    allowed = set(ROLE_ALLOWED_BLOCKS[role])
    return {name: values for name, values in blocks.items() if name in allowed and values}


def render_context_text(blocks: dict[str, list[str]], *, workspace_id: str, query: str) -> str:
    lines = [
        "[LONG-TERM CONTEXT]",
        "",
        "Note: The following memory may be outdated. Always prioritize explicit user instructions and current tool outputs.",
        "",
    ]
    if "Relevant Preferences" in blocks:
        lines.append("Relevant Preferences:")
        lines.extend(f"- {item}" for item in blocks["Relevant Preferences"])
        lines.append("")
    if "Workspace Facts" in blocks:
        lines.append("Workspace Facts:")
        lines.extend(f"- {item}" for item in blocks["Workspace Facts"])
        lines.append("")
    if "Prior Decisions" in blocks:
        lines.append("Prior Decisions:")
        lines.extend(f"- {item}" for item in blocks["Prior Decisions"])
        lines.append("")
    lines.append("Task Continuation State:")
    lines.append(f"- workspace: {workspace_id}")
    if query:
        lines.append(f"- current_task: {query[:220]}")
    for item in blocks.get("Task Continuation State", []):
        lines.append(f"- {item}")
    lines.append("")
    if "Known Risks" in blocks:
        lines.append("Known Risks:")
        lines.extend(f"- {item}" for item in blocks["Known Risks"])
        lines.append("")
    if "Retrieved Facts" in blocks:
        lines.append("Retrieved Facts:")
        lines.extend(f"- {item}" for item in blocks["Retrieved Facts"])
    return "\n".join(lines).strip()


def inject_context(payload: dict[str, Any], context_text: str) -> dict[str, Any]:
    new_payload = json.loads(json.dumps(payload))
    if "input" in new_payload:
        input_value = new_payload["input"]
        context_item = {
            "role": "system",
            "content": [
                {
                    "type": "input_text",
                    "text": context_text,
                }
            ],
        }
        if isinstance(input_value, list):
            new_payload["input"] = [context_item, *input_value]
        elif isinstance(input_value, str):
            new_payload["input"] = [
                context_item,
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": input_value,
                        }
                    ],
                },
            ]
    elif "messages" in new_payload and isinstance(new_payload["messages"], list):
        new_payload["messages"] = [{"role": "system", "content": context_text}, *new_payload["messages"]]
    return new_payload


def build_state_key(user_id: str, workspace_id: str) -> str:
    return f"{user_id}:{workspace_id}"


def update_run_state(
    *,
    user_id: str,
    workspace_id: str,
    role: str | None,
    trigger: str,
    context_blocks: dict[str, list[str]],
    query: str,
    fallback: str | None = None,
) -> dict[str, Any]:
    state = load_state()
    key = build_state_key(user_id, workspace_id)
    bucket = state.get(
        key,
        {
            "user_id": user_id,
            "workspace_id": workspace_id,
            "active_roles": [],
            "fallback_history": [],
            "memory_budget": {},
            "task_summary": "",
        },
    )
    if role and role not in bucket["active_roles"]:
        bucket["active_roles"].append(role)
    bucket["last_trigger"] = trigger
    bucket["last_query"] = query[:220]
    bucket["last_context_blocks"] = context_blocks
    bucket["updated_at"] = int(time.time())
    if fallback:
        bucket["fallback_history"] = [fallback, *bucket.get("fallback_history", [])][:5]
    state[key] = bucket
    save_state(state)
    return bucket


def build_context_package(
    config: GatewayConfig,
    payload: dict[str, Any],
    headers: dict[str, str],
) -> dict[str, Any]:
    metadata = payload.get("metadata", {}) if isinstance(payload.get("metadata"), dict) else {}
    role = headers.get("x-agent-role") or metadata.get("role")
    trigger = headers.get("x-memory-trigger") or metadata.get("memory_trigger") or "before_task_start"
    user_id = headers.get("x-openmemory-user") or metadata.get("user_id") or config.default_user_id
    workspace_id = headers.get("x-codex-workspace") or metadata.get("workspace_id") or config.default_workspace_id
    query = extract_query(payload)
    blocks = build_context_blocks(
        config,
        user_id=user_id,
        workspace_id=workspace_id,
        role=role,
        query=query,
        trigger=trigger,
    )
    role_blocks = select_role_blocks(blocks, role)
    context_text = render_context_text(role_blocks, workspace_id=workspace_id, query=query)
    run_state = update_run_state(
        user_id=user_id,
        workspace_id=workspace_id,
        role=role,
        trigger=trigger,
        context_blocks=role_blocks,
        query=query,
    )
    return {
        "role": role,
        "trigger": trigger,
        "user_id": user_id,
        "workspace_id": workspace_id,
        "query": query,
        "context_blocks": role_blocks,
        "context_text": context_text,
        "run_state": run_state,
    }


def parse_response_text(response_json: dict[str, Any]) -> str | None:
    if isinstance(response_json.get("output_text"), str):
        return response_json["output_text"]
    output = response_json.get("output")
    if not isinstance(output, list):
        return None
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts).strip() or None


def maybe_parse_structured_output(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and "state_delta" in payload and "fallback_suggestion" in payload:
        return payload
    return None


def materialize_payloads(payloads: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[Path], tempfile.TemporaryDirectory[str]]:
    temp_dir = tempfile.TemporaryDirectory()
    files: list[Path] = []
    valid_payloads: list[dict[str, Any]] = []
    for index, payload in enumerate(payloads):
        file_path = Path(temp_dir.name) / f"payload_{index}.json"
        file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        files.append(file_path)
        valid_payloads.append(payload)
    return valid_payloads, files, temp_dir


def writeback_approved_items(
    config: GatewayConfig,
    *,
    user_id: str,
    workspace_id: str,
    merged_state: dict[str, list[dict[str, Any]]],
    approval_report: dict[str, Any],
) -> dict[str, Any]:
    existing = list_memories(config, user_id)
    existing_by_identity: dict[str, list[dict[str, Any]]] = {}
    for memory in existing:
        metadata = memory.get("metadata", {}) or {}
        identity = metadata.get("identity")
        if identity and metadata.get("workspace_id", workspace_id) == workspace_id:
            existing_by_identity.setdefault(identity, []).append(memory)

    merged_index = {
        item["identity"]: item
        for key in ("facts", "preferences", "decisions", "risks")
        for item in merged_state.get(key, [])
    }

    writeback: list[dict[str, Any]] = []
    for approved in approval_report.get("approved", []):
        identity = approved["identity"]
        item = merged_index.get(identity)
        if not item:
            continue
        for previous in existing_by_identity.get(identity, []):
            proposal_type = approved.get("proposal_type")
            if proposal_type in {"update", "invalidate"}:
                update_memory(config, previous["id"], state="archived")
        if approved.get("proposal_type") == "invalidate":
            writeback.append({"identity": identity, "action": "archived_previous_only"})
            continue

        content = render_memory_content(item)
        metadata = {
            "protocol": "v1.1",
            "scope": item.get("scope", approved.get("scope")),
            "kind": item.get("kind"),
            "identity": identity,
            "proposal_type": approved.get("proposal_type"),
            "source": item.get("source"),
            "workspace_id": workspace_id,
            "status": "approved",
            "approved_by": "main_agent_gateway",
            "evidence_ids": item.get("evidence_ids", []),
            "source_files": approved.get("source_files", []),
            "approved_at": int(time.time()),
        }
        created = add_memory(config, user_id, content, metadata)
        writeback.append({"identity": identity, "action": "created", "memory": created})

    if config.store_audit_records and approval_report.get("audit_records"):
        audit_content = json.dumps(
            {
                "approved": approval_report.get("approved", []),
                "deferred": approval_report.get("deferred", []),
                "rejected": approval_report.get("rejected", []),
            },
            ensure_ascii=False,
        )
        audit_metadata = {
            "protocol": "v1.1",
            "kind": "audit_record",
            "scope": "task",
            "workspace_id": workspace_id,
            "status": "approved",
            "approved_by": "main_agent_gateway",
            "approved_at": int(time.time()),
        }
        created = add_memory(config, user_id, audit_content, audit_metadata)
        writeback.append({"identity": "audit_record", "action": "created", "memory": created})

    return {"writeback": writeback}


def render_memory_content(item: dict[str, Any]) -> str:
    kind = item.get("kind")
    if kind in {"facts", "preferences"}:
        return f"{item.get('key')}: {item.get('value')}"
    if kind == "decisions":
        return f"[{item.get('proposal_type')}] {item.get('topic')}: {item.get('decision')}"
    if kind == "risks":
        return str(item.get("risk"))
    return item.get("legacy_text", "")


def approve_payloads(
    config: GatewayConfig,
    *,
    payloads: list[dict[str, Any]],
    user_id: str,
    workspace_id: str,
    writeback: bool,
) -> dict[str, Any]:
    valid_payloads, files, temp_dir = materialize_payloads(payloads)
    try:
        validation_errors: list[str] = []
        accepted_payloads: list[dict[str, Any]] = []
        accepted_files: list[Path] = []
        for payload, file_path in zip(valid_payloads, files, strict=False):
            errors = VALIDATOR.validate_payload(payload, file_path)
            validation_errors.extend(errors)
            if not errors:
                accepted_payloads.append(payload)
                accepted_files.append(file_path)

        merged_state = VALIDATOR.merge_payloads(accepted_payloads, accepted_files) if accepted_payloads else {}
        approval_report = VALIDATOR.build_approval_report(merged_state) if accepted_payloads else {"approved": [], "deferred": [], "rejected": [], "audit_records": [], "ok": False}
        fallback_report = VALIDATOR.build_fallback_report(accepted_payloads, accepted_files) if accepted_payloads else {"ok": True, "routes": []}
        role_context_report = VALIDATOR.build_role_context_report(accepted_payloads, accepted_files) if accepted_payloads else {"ok": True, "per_file": []}
        budget_report = VALIDATOR.build_budget_report(
            accepted_payloads,
            accepted_files,
            max_stage_context_tokens=config.max_stage_context_tokens,
            max_long_term_tokens=config.max_long_term_context_tokens,
        ) if accepted_payloads else {"ok": True, "violations": []}
        assertions = VALIDATOR.build_contract_assertions(
            accepted_payloads,
            role_context_report=role_context_report,
            budget_report=budget_report,
            approval_report=approval_report,
            fallback_report=fallback_report,
        ) if accepted_payloads else {}

        result: dict[str, Any] = {
            "ok": not validation_errors and all(assertions.values()) if assertions else False,
            "validation_errors": validation_errors,
            "merged_state": merged_state,
            "approval_report": approval_report,
            "fallback_report": fallback_report,
            "role_context_report": role_context_report,
            "budget_report": budget_report,
            "contract_assertions": assertions,
        }

        if writeback and approval_report.get("approved") and config.writeback_enabled:
            result["writeback_report"] = writeback_approved_items(
                config,
                user_id=user_id,
                workspace_id=workspace_id,
                merged_state=merged_state,
                approval_report=approval_report,
            )
        return result
    finally:
        temp_dir.cleanup()


def proxy_request(
    config: GatewayConfig,
    *,
    path: str,
    method: str,
    headers: dict[str, str],
    body: bytes | None,
) -> tuple[int, dict[str, str], bytes]:
    url = f"{config.upstream_base_url}{path}"
    upstream_headers = {key: value for key, value in headers.items() if key.lower() not in {"host", "content-length"}}
    request = urllib.request.Request(url, data=body, headers=upstream_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=config.upstream_timeout_seconds) as response:
            response_body = response.read()
            response_headers = dict(response.headers.items())
            return response.status, response_headers, response_body
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers.items()), error.read()


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "MainAgentGateway/1.1"

    @property
    def config(self) -> GatewayConfig:
        return self.server.gateway_config  # type: ignore[attr-defined]

    def _json_response(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length) if content_length > 0 else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            state = load_state()
            self._json_response(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "upstream_base_url": self.config.upstream_base_url,
                    "default_workspace_id": self.config.default_workspace_id,
                    "writeback_enabled": self.config.writeback_enabled,
                    "run_state_keys": sorted(state.keys()),
                },
            )
            return

        if self.path == "/v1/models":
            status, headers, body = proxy_request(
                self.config,
                path="/models",
                method="GET",
                headers={key: value for key, value in self.headers.items()},
                body=None,
            )
            self.send_response(status)
            for key, value in headers.items():
                if key.lower() == "transfer-encoding":
                    continue
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
            return

        self._json_response(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/v1/main-agent/context":
            payload = self._read_json()
            package = build_context_package(
                self.config,
                payload,
                {key.lower(): value for key, value in self.headers.items()},
            )
            self._json_response(HTTPStatus.OK, package)
            return

        if self.path == "/v1/main-agent/approve":
            payload = self._read_json()
            package = approve_payloads(
                self.config,
                payloads=payload.get("payloads", []),
                user_id=payload.get("user_id", self.config.default_user_id),
                workspace_id=payload.get("workspace_id", self.config.default_workspace_id),
                writeback=bool(payload.get("writeback", False)),
            )
            self._json_response(HTTPStatus.OK, package)
            return

        if self.path == "/v1/responses":
            payload = self._read_json()
            package = build_context_package(
                self.config,
                payload,
                {key.lower(): value for key, value in self.headers.items()},
            )
            proxied_payload = inject_context(payload, package["context_text"])
            raw_body = json.dumps(proxied_payload, ensure_ascii=False).encode("utf-8")
            status, headers, body = proxy_request(
                self.config,
                path="/responses",
                method="POST",
                headers={key: value for key, value in self.headers.items()},
                body=raw_body,
            )
            if headers.get("Content-Type", "").startswith("application/json"):
                response_json = json.loads(body.decode("utf-8") or "{}")
                structured = maybe_parse_structured_output(parse_response_text(response_json))
                if structured is not None:
                    approval = approve_payloads(
                        self.config,
                        payloads=[structured],
                        user_id=package["user_id"],
                        workspace_id=package["workspace_id"],
                        writeback=True,
                    )
                    response_json["_main_agent_gateway"] = {
                        "context_blocks": package["context_blocks"],
                        "approval": approval,
                    }
                    if structured.get("fallback_suggestion"):
                        update_run_state(
                            user_id=package["user_id"],
                            workspace_id=package["workspace_id"],
                            role=package["role"],
                            trigger=package["trigger"],
                            context_blocks=package["context_blocks"],
                            query=package["query"],
                            fallback=structured["fallback_suggestion"],
                        )
                self._json_response(status, response_json)
                return

            self.send_response(status)
            for key, value in headers.items():
                if key.lower() == "transfer-encoding":
                    continue
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
            return

        self._json_response(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        message = "%s - - [%s] %s\n" % (
            self.address_string(),
            self.log_date_time_string(),
            format % args,
        )
        print(message, end="", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Main agent gateway with v1.1 memory orchestration")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(Path(args.config))
    ensure_runtime_paths()
    server = ThreadingHTTPServer((config.host, config.port), GatewayHandler)
    server.gateway_config = config  # type: ignore[attr-defined]
    print(f"main_agent_gateway listening on http://{config.host}:{config.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
