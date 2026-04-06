#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import main_agent_runtime as runtime


REPORT_PATH = Path(__file__).resolve().parent / "runtime" / "acceptance" / "compactor_dry_run_acceptance_report.json"


def add_fixture_memory(
    config: runtime.RuntimeConfig,
    *,
    user_id: str,
    workspace_id: str,
    key: str,
    value: str,
    source: str = "tool_output",
) -> dict:
    metadata = {
        "protocol": "v1.1",
        "kind": "facts",
        "scope": "workspace",
        "source": source,
        "identity": f"facts:workspace:{key}",
        "workspace_id": workspace_id,
        "status": "approved",
        "approved_by": "compactor_acceptance",
        "approved_at": int(time.time()),
        "evidence_ids": [f"fixture-{key}-e1"],
        "source_files": [str(REPORT_PATH)],
        "confidence_score": 0.9,
    }
    content = f"{key}: {value}"
    return runtime.add_memory(config, user_id, content, metadata)


def archive_fixtures(config: runtime.RuntimeConfig, fixture_ids: list[str]) -> None:
    for memory_id in fixture_ids:
        try:
            runtime.update_memory(config, memory_id, state="archived")
        except Exception:
            continue


def active_workspace_memory_ids(config: runtime.RuntimeConfig, *, user_id: str, workspace_id: str) -> set[str]:
    ids: set[str] = set()
    for memory in runtime.list_memories(config, user_id):
        metadata = memory.get("metadata", {}) or {}
        if metadata.get("workspace_id") != workspace_id:
            continue
        if memory.get("state") != "active":
            continue
        if memory.get("id"):
            ids.add(str(memory["id"]))
    return ids


def main() -> int:
    config = runtime.load_config(runtime.CONFIG_PATH.resolve())
    user_id = config.default_user_id
    workspace_id = f"compactor-dry-run-acceptance-{uuid.uuid4().hex[:8]}"
    fixture_ids: list[str] = []
    report: dict = {"ok": False}

    try:
        source_a = add_fixture_memory(
            config,
            user_id=user_id,
            workspace_id=workspace_id,
            key="compactor_fixture_alpha",
            value="User repeatedly records approved workspace facts before compressing them.",
        )
        source_b = add_fixture_memory(
            config,
            user_id=user_id,
            workspace_id=workspace_id,
            key="compactor_fixture_beta",
            value="Approved workspace facts must keep evidence and rollback paths after compaction.",
        )
        ignored = add_fixture_memory(
            config,
            user_id=user_id,
            workspace_id=workspace_id,
            key="compactor_fixture_gamma",
            value="This memory should remain unselected because explicit IDs are used.",
        )
        fixture_ids = [str(source_a["id"]), str(source_b["id"]), str(ignored["id"])]
        before_ids = active_workspace_memory_ids(config, user_id=user_id, workspace_id=workspace_id)

        command = [
            sys.executable,
            str(Path(__file__).resolve().parent / "main_agent_runtime.py"),
            "compact",
            "--dry-run",
            "--workspace-id",
            workspace_id,
            "--memory-id",
            fixture_ids[0],
            "--memory-id",
            fixture_ids[1],
            "--key",
            "compactor_acceptance_rule",
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        parsed = json.loads(completed.stdout)
        after_ids = active_workspace_memory_ids(config, user_id=user_id, workspace_id=workspace_id)

        report = {
            "ok": (
                completed.returncode == 0
                and parsed.get("ok") is True
                and parsed.get("proposal", {}).get("schema_version") == "compression_proposal_v1"
                and not parsed.get("validation_errors")
                and len(parsed.get("approval_report", {}).get("approved", [])) == 1
                and parsed.get("proposal", {}).get("state_delta", {}).get("decisions") == []
                and parsed.get("proposal", {}).get("state_delta", {}).get("preferences") == []
                and parsed.get("proposal", {}).get("compression_manifest", {}).get("target_workspace_id") == workspace_id
                and parsed.get("proposal", {}).get("compression_manifest", {}).get("source_memory_ids") == fixture_ids[:2]
                and "raw_audit_trail_hashes" in parsed.get("proposal", {}).get("compression_manifest", {})
                and before_ids == after_ids
            ),
            "workspace_id": workspace_id,
            "command": command,
            "returncode": completed.returncode,
            "stderr": completed.stderr,
            "result": parsed,
            "before_active_ids": sorted(before_ids),
            "after_active_ids": sorted(after_ids),
        }
    finally:
        archive_fixtures(config, fixture_ids)

    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
