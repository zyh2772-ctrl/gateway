#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "main_agent_runtime.py"
DEFAULT_CASES = ROOT / "runtime" / "acceptance" / "recall_baseline_cases_v1.json"
DEFAULT_REPORT = ROOT / "runtime" / "acceptance" / "recall_baseline_report_v1.json"


def load_cases(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise RuntimeError("baseline cases must be a JSON array")
    return raw


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    name = case.get("name", "unnamed")
    args = case.get("args", [])
    if not isinstance(args, list):
        raise RuntimeError(f"{name}: args must be a list")
    cmd = [sys.executable, str(RUNTIME), "recall-debug", *[str(item) for item in args]]
    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    failures: list[str] = []
    payload: dict[str, Any] | None = None
    if completed.returncode != 0:
        failures.append(f"command_failed:{completed.returncode}")
    else:
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                payload = parsed
            else:
                failures.append("output_not_json_object")
        except json.JSONDecodeError as exc:
            failures.append(f"invalid_json_output:{exc}")
    if payload is None:
        return {
            "name": name,
            "ok": False,
            "command": cmd,
            "failures": failures,
            "stdout": stdout,
            "stderr": stderr,
        }

    expectations = case.get("expectations", {})
    failures.extend(validate_case(payload, expectations))
    return {
        "name": name,
        "ok": not failures,
        "command": cmd,
        "failures": failures,
        "stderr": stderr,
        "observed": summarize_payload(payload),
    }


def validate_case(payload: dict[str, Any], expectations: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    semantic_status = (((payload.get("debug") or {}).get("semantic_backend") or {}).get("status"))
    if expectations.get("semantic_backend_status") and semantic_status != expectations["semantic_backend_status"]:
        failures.append(
            f"semantic_backend_status expected={expectations['semantic_backend_status']} actual={semantic_status}"
        )

    expected_trigger = expectations.get("trigger")
    if expected_trigger and payload.get("trigger") != expected_trigger:
        failures.append(f"trigger expected={expected_trigger} actual={payload.get('trigger')}")

    run_state_stage = (((payload.get("run_state") or {}).get("lifecycle_stage")))
    if expectations.get("run_state_stage") and run_state_stage != expectations["run_state_stage"]:
        failures.append(f"run_state_stage expected={expectations['run_state_stage']} actual={run_state_stage}")

    context_blocks = payload.get("context_blocks") or {}
    actual_selected_blocks = list(context_blocks.keys())
    expected_selected_blocks = expectations.get("selected_blocks")
    if isinstance(expected_selected_blocks, list) and actual_selected_blocks != expected_selected_blocks:
        failures.append(
            f"selected_blocks expected={expected_selected_blocks} actual={actual_selected_blocks}"
        )

    per_memory = ((payload.get("debug") or {}).get("per_memory") or [])
    status_index: dict[str, dict[str, Any]] = {}
    for item in per_memory:
        identity = item.get("identity")
        if isinstance(identity, str) and identity:
            status_index[identity] = item

    for identity in expectations.get("selected_identities", []):
        item = status_index.get(identity)
        if not item:
            failures.append(f"missing_identity:{identity}")
            continue
        if item.get("status") != "selected":
            failures.append(f"identity_not_selected:{identity}:{item.get('status')}")

    for identity, reason in (expectations.get("filtered_reasons") or {}).items():
        item = status_index.get(identity)
        if not item:
            failures.append(f"missing_identity:{identity}")
            continue
        if item.get("status") != "filtered":
            failures.append(f"identity_not_filtered:{identity}:{item.get('status')}")
            continue
        if item.get("reason") != reason:
            failures.append(f"filtered_reason_mismatch:{identity}:expected={reason}:actual={item.get('reason')}")

    for identity, gate in (expectations.get("selected_retrieval_gates") or {}).items():
        item = status_index.get(identity)
        if not item:
            failures.append(f"missing_identity:{identity}")
            continue
        if item.get("retrieval_gate") != gate:
            failures.append(
                f"retrieval_gate_mismatch:{identity}:expected={gate}:actual={item.get('retrieval_gate')}"
            )

    for identity in expectations.get("low_confidence_identities", []):
        item = status_index.get(identity)
        if not item:
            failures.append(f"missing_identity:{identity}")
            continue
        if not ((item.get("scores") or {}).get("low_confidence")):
            failures.append(f"low_confidence_expected:{identity}")

    for block_name, snippets in (expectations.get("selected_text_contains") or {}).items():
        values = context_blocks.get(block_name) or []
        block_text = "\n".join(str(item) for item in values)
        for snippet in snippets:
            if snippet not in block_text:
                failures.append(f"text_missing:{block_name}:{snippet}")

    return failures


def summarize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    per_memory = ((payload.get("debug") or {}).get("per_memory") or [])
    selected_identities: list[str] = []
    filtered_identities: dict[str, str | None] = {}
    low_confidence_selected: list[str] = []
    for item in per_memory:
        identity = item.get("identity")
        if not isinstance(identity, str) or not identity:
            continue
        status = item.get("status")
        if status == "selected":
            selected_identities.append(identity)
            if ((item.get("scores") or {}).get("low_confidence")):
                low_confidence_selected.append(identity)
        elif status == "filtered":
            filtered_identities[identity] = item.get("reason")
    return {
        "trigger": payload.get("trigger"),
        "run_revision": payload.get("run_revision"),
        "context_snapshot_id": payload.get("context_snapshot_id"),
        "selected_blocks": list((payload.get("context_blocks") or {}).keys()),
        "semantic_backend": (payload.get("debug") or {}).get("semantic_backend"),
        "run_state_stage": ((payload.get("run_state") or {}).get("lifecycle_stage")),
        "selected_identities": selected_identities,
        "low_confidence_selected": low_confidence_selected,
        "filtered_identities": filtered_identities,
    }


def main() -> int:
    cases_path = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_CASES
    report_path = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else DEFAULT_REPORT
    cases = load_cases(cases_path)
    results = [run_case(case) for case in cases]
    report = {
      "ok": all(result["ok"] for result in results),
      "cases_path": str(cases_path),
      "results": results,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
