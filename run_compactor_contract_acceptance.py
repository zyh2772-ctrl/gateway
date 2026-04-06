#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import main_agent_runtime as runtime


REPORT_PATH = Path(__file__).resolve().parent / "runtime" / "acceptance" / "compactor_contract_acceptance_report.json"
COMPACTOR_PROMPT = (
    Path(__file__).resolve().parent.parent
    / "codex-global-multi-agent"
    / "prompts"
    / "roles"
    / "compactor.md"
)
ROLES_DOC = Path(__file__).resolve().parent.parent / "codex-global-multi-agent" / "ROLES_AND_SKILLS.md"


def build_compactor_schema_fixture() -> dict:
    return {
        "role": "Compactor",
        "memory_access_mode": "main_agent_packaged_context_only",
        "context_snapshot_id": "compactor-contract-acceptance-snap",
        "run_revision": 1,
        "context_blocks": [
            "Workspace Facts",
            "Prior Decisions",
            "Known Risks",
        ],
        "source_context_blocks": [
            "Workspace Facts",
            "Prior Decisions",
            "Known Risks",
        ],
        "goal": "validate compactor role contract accepts legal compression proposal",
        "scope": "workspace",
        "allowed_tools": [
            "memory_search",
            "memory_read",
        ],
        "summary": "Compactor contract acceptance fixture.",
        "context_token_estimate": 180,
        "evidence": [
            {
                "id": "compactor-contract-e1",
                "type": "memory",
                "value": "Approved memories selected for compaction",
                "ref": "memory-a,memory-b",
                "source": "approved_decision",
            }
        ],
        "state_delta": {
            "facts": [
                {
                    "key": "compactor_contract_fixture",
                    "value": "[Derived] Contract fixture keeps rollback and evidence fields intact.",
                    "scope": "workspace",
                    "source": "approved_decision",
                    "evidence_ids": ["compactor-contract-e1"],
                }
            ],
            "preferences": [],
            "decisions": [],
            "risks": [],
        },
        "risks": [
            "Contract fixture only validates schema and role routing.",
        ],
        "fallback_suggestion": "escalate_to_main_agent",
        "next_steps": [
            "Review compactor role contract.",
        ],
        "confidence": 0.8,
        "schema_version": "compression_proposal_v1",
        "proposal_kind": "compression_proposal",
        "compression_manifest": {
            "target_workspace_id": "ollamashiyong",
            "target_scope": "workspace",
            "rollback_basis": "raw_audit_trail_hashes + source_memory_ids",
            "source_evidence_hash": "sha256:contract-fixture",
            "source_memory_ids": ["memory-a", "memory-b"],
            "source_identities": ["facts:workspace:a", "facts:workspace:b"],
            "raw_audit_trail_hashes": ["sha256:audit-a", "sha256:audit-b"],
            "conflict_class": "none",
            "risk_resolution_mode": "retain",
            "defer_reason": "",
        },
    }


def main() -> int:
    config = runtime.load_config(runtime.CONFIG_PATH.resolve())
    prompt_text = COMPACTOR_PROMPT.read_text(encoding="utf-8")
    roles_text = ROLES_DOC.read_text(encoding="utf-8")
    recall_result = runtime.build_context_package(
        config,
        user_id=config.default_user_id,
        workspace_id="ollamashiyong",
        role="Compactor",
        query="contract acceptance for compactor role",
        trigger="before_task_start",
    )
    fixture = build_compactor_schema_fixture()
    validation_errors = runtime.VALIDATOR.validate_payload(
        fixture,
        Path("compactor_contract_acceptance.json"),
    )
    report = {
        "ok": (
            "compression_proposal_v1" in prompt_text
            and "raw_audit_trail_hashes" in prompt_text
            and "source_evidence_hash" in prompt_text
            and "rollback_basis" in prompt_text
            and "[Defer Compaction]" in prompt_text
            and "Compactor" in roles_text
            and recall_result.get("budget_profile", {}).get("role_multiplier") == 0.75
            and set(recall_result.get("source_context_blocks", [])).issubset(
                {"Workspace Facts", "Prior Decisions", "Known Risks", "Retrieved Facts"}
            )
            and not validation_errors
        ),
        "prompt_path": str(COMPACTOR_PROMPT),
        "roles_doc_path": str(ROLES_DOC),
        "recall_result": {
            "role": recall_result.get("role"),
            "source_context_blocks": recall_result.get("source_context_blocks"),
            "budget_profile": recall_result.get("budget_profile"),
        },
        "validation_errors": validation_errors,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
