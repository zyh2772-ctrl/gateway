#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "main_agent_runtime.py"
COMPAT = ROOT.parent / "codex-global-memory" / "openmemory" / "openmemory_db_compat.py"
REPORT_PATH = ROOT / "runtime" / "acceptance" / "context_override_envelope_acceptance_report.json"

USER_ID = "zyh"
WORKSPACE_ID = "context-override-envelope-acceptance"
PREFERENCE_KEY = "context_override_envelope_pref"
IDENTITY = f"preferences:workspace:{PREFERENCE_KEY}"


def run_json(cmd: list[str], *, stdin_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    completed = subprocess.run(
        cmd,
        input=(json.dumps(stdin_payload, ensure_ascii=False) if stdin_payload is not None else None),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            json.dumps(
                {
                    "command": cmd,
                    "returncode": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid json output from {cmd}: {exc}\nstdout={completed.stdout}") from exc


def list_workspace_memories() -> list[dict[str, Any]]:
    rows = run_json([sys.executable, str(COMPAT), "list", "--user", USER_ID])
    if not isinstance(rows, list):
        raise RuntimeError("memory list response is not a list")
    return [row for row in rows if ((row.get("metadata") or {}).get("workspace_id") == WORKSPACE_ID)]


def archive_memory(memory_id: str) -> dict[str, Any]:
    return run_json([sys.executable, str(COMPAT), "update", "--id", memory_id, "--state", "archived"])


def cleanup_workspace() -> list[str]:
    archived_ids: list[str] = []
    for row in list_workspace_memories():
        if row.get("state") != "active":
            continue
        archive_memory(row["id"])
        archived_ids.append(row["id"])
    return archived_ids


def seed_preference(value: str) -> dict[str, Any]:
    metadata = {
        "kind": "preferences",
        "scope": "workspace",
        "workspace_id": WORKSPACE_ID,
        "identity": IDENTITY,
        "status": "approved",
        "approved_by": "acceptance_test",
        "source": "observed_fact",
        "confidence_score": 0.95,
        "test_fixture": True,
    }
    return run_json(
        [
            sys.executable,
            str(COMPAT),
            "add",
            "--user",
            USER_ID,
            "--app",
            "openmemory",
            "--content",
            f"{PREFERENCE_KEY}: {value}",
            "--metadata",
            json.dumps(metadata, ensure_ascii=False),
        ]
    )


def assert_stale_item(context_package: dict[str, Any], *, mode: str) -> dict[str, Any]:
    stale_items = context_package.get("stale_or_superseded") or []
    matches = [item for item in stale_items if item.get("identity") == IDENTITY and item.get("override_mode") == mode]
    if not matches:
        raise RuntimeError(
            f"expected stale_or_superseded item for {IDENTITY} mode={mode}, got {json.dumps(stale_items, ensure_ascii=False)}"
        )
    return matches[0]


def build_preference_payload(value: str, *, context_package: dict[str, Any]) -> dict[str, Any]:
    evidence_id = f"{PREFERENCE_KEY}-e1-{value}"
    return {
        "role": "Planner",
        "memory_access_mode": "main_agent_packaged_context_only",
        "context_blocks": ["Relevant Preferences"],
        "context_snapshot_id": context_package.get("context_snapshot_id"),
        "run_revision": context_package.get("run_revision"),
        "goal": f"Update {PREFERENCE_KEY} in the dedicated envelope acceptance workspace.",
        "scope": "workspace",
        "allowed_tools": ["exec_command"],
        "summary": f"Replace {PREFERENCE_KEY} with {value} after the main agent identified an override candidate.",
        "evidence": [
            {
                "id": evidence_id,
                "type": "command",
                "value": f"Envelope acceptance for {PREFERENCE_KEY} -> {value}",
                "ref": str(REPORT_PATH),
                "source": "Planner",
                "timestamp": "2026-04-06",
            }
        ],
        "state_delta": {
            "facts": [],
            "preferences": [
                {
                    "key": PREFERENCE_KEY,
                    "value": value,
                    "scope": "workspace",
                    "proposal_type": "update",
                    "source": "observed_fact",
                    "evidence_ids": [evidence_id],
                }
            ],
            "decisions": [],
            "risks": [],
        },
        "risks": [],
        "fallback_suggestion": "retry_same_tool",
        "next_steps": ["Main agent should archive the superseded value and create the approved replacement."],
        "confidence": 0.93,
    }


def filter_identity(memories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in memories if ((item.get("metadata") or {}).get("identity") == IDENTITY)]


def main() -> int:
    report: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "identity": IDENTITY,
        "steps": {},
        "cleanup": {},
    }

    pre_cleanup_ids = cleanup_workspace()
    report["cleanup"]["pre_cleanup_archived_ids"] = pre_cleanup_ids

    seeded = seed_preference("blue")
    report["steps"]["seed"] = seeded

    replace_context = run_json(
        [
            sys.executable,
            str(RUNTIME),
            "recall",
            "--workspace-id",
            WORKSPACE_ID,
            "--role",
            "Planner",
            "--trigger",
            "before_task_start",
            "--query",
            f"{PREFERENCE_KEY} 改为 red",
        ]
    )
    report["steps"]["replace_recall"] = {
        "context_snapshot_id": replace_context.get("context_snapshot_id"),
        "run_revision": replace_context.get("run_revision"),
        "stale_or_superseded": replace_context.get("stale_or_superseded"),
        "context_blocks": replace_context.get("context_blocks"),
    }
    assert_stale_item(replace_context, mode="replace")

    replace_envelope = {
        "context_package": replace_context,
        "payloads": [build_preference_payload("red", context_package=replace_context)],
    }
    replace_result = run_json(
        [
            sys.executable,
            str(RUNTIME),
            "approve",
            "--workspace-id",
            WORKSPACE_ID,
            "--writeback",
        ],
        stdin_payload=replace_envelope,
    )
    report["steps"]["replace_approve"] = replace_result
    if replace_result.get("input_bundle", {}).get("context_package_source") != "envelope":
        raise RuntimeError("replace approve did not use envelope context_package source")
    replace_actions = (replace_result.get("context_override_report") or {}).get("actions") or []
    if not any(action.get("identity") == IDENTITY and action.get("strategy") == "archive_then_create" for action in replace_actions):
        raise RuntimeError(f"replace context override strategy mismatch: {json.dumps(replace_actions, ensure_ascii=False)}")

    after_replace = filter_identity(list_workspace_memories())
    report["steps"]["after_replace"] = after_replace
    active_replace = [item for item in after_replace if item.get("state") == "active"]
    if len(active_replace) != 1 or active_replace[0].get("content") != f"{PREFERENCE_KEY}: red":
        raise RuntimeError(f"replace writeback verification failed: {json.dumps(after_replace, ensure_ascii=False)}")

    negate_context = run_json(
        [
            sys.executable,
            str(RUNTIME),
            "recall",
            "--workspace-id",
            WORKSPACE_ID,
            "--role",
            "Planner",
            "--trigger",
            "before_task_start",
            "--query",
            f"{PREFERENCE_KEY} 不再使用",
        ]
    )
    report["steps"]["negate_recall"] = {
        "context_snapshot_id": negate_context.get("context_snapshot_id"),
        "run_revision": negate_context.get("run_revision"),
        "stale_or_superseded": negate_context.get("stale_or_superseded"),
        "context_blocks": negate_context.get("context_blocks"),
    }
    assert_stale_item(negate_context, mode="negate")

    negate_result = run_json(
        [
            sys.executable,
            str(RUNTIME),
            "approve",
            "--workspace-id",
            WORKSPACE_ID,
            "--writeback",
        ],
        stdin_payload={"context_package": negate_context, "payloads": []},
    )
    report["steps"]["negate_approve"] = negate_result
    if negate_result.get("input_bundle", {}).get("context_package_source") != "envelope":
        raise RuntimeError("negate approve did not use envelope context_package source")
    negate_actions = (negate_result.get("context_override_report") or {}).get("actions") or []
    if not any(action.get("identity") == IDENTITY and action.get("strategy") == "archive_only" for action in negate_actions):
        raise RuntimeError(f"negate context override strategy mismatch: {json.dumps(negate_actions, ensure_ascii=False)}")

    after_negate = filter_identity(list_workspace_memories())
    report["steps"]["after_negate"] = after_negate
    if any(item.get("state") == "active" for item in after_negate):
        raise RuntimeError(f"negate writeback verification failed: {json.dumps(after_negate, ensure_ascii=False)}")

    post_cleanup_ids = cleanup_workspace()
    report["cleanup"]["post_cleanup_archived_ids"] = post_cleanup_ids
    report["ok"] = True
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
