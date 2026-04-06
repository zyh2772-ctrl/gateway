#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any


ALLOWED_SOURCE_KINDS = {
    "facts",
    "decisions",
    "compressed_fact",
    "derived_fact",
}

SKIPPED_REASON_PRIORITY = (
    "requested_memory_not_found",
    "memory_id_not_requested",
    "workspace_scope_mismatch",
    "inactive_memory",
    "missing_identity",
    "essential_memory",
    "status_not_approved",
    "unsupported_kind",
)


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def build_memory_fingerprint(memory: dict[str, Any]) -> str:
    metadata = memory.get("metadata", {}) or {}
    raw = {
        "id": memory.get("id"),
        "content": memory.get("content"),
        "updated_at": memory.get("updated_at"),
        "metadata": metadata,
    }
    digest = hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def format_time_window(memories: list[dict[str, Any]]) -> str:
    timestamps: list[int] = []
    for memory in memories:
        for key in ("created_at", "updated_at"):
            value = memory.get(key)
            if isinstance(value, (int, float)):
                timestamps.append(int(value))
    if not timestamps:
        today = dt.date.today().isoformat()
        return f"{today}/{today}"
    start = dt.datetime.fromtimestamp(min(timestamps)).date().isoformat()
    end = dt.datetime.fromtimestamp(max(timestamps)).date().isoformat()
    return f"{start}/{end}"


def default_compaction_key(workspace_id: str, source_memory_ids: list[str]) -> str:
    raw = f"{workspace_id}:{','.join(source_memory_ids)}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"compacted_{workspace_id.replace('-', '_')}_{digest}"


def render_source_preview(memory: dict[str, Any], *, limit: int = 96) -> str:
    text = normalize_whitespace(str(memory.get("content") or ""))
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def build_compacted_value(memories: list[dict[str, Any]], *, target_scope: str) -> str:
    previews = [render_source_preview(memory) for memory in memories[:3] if memory.get("content")]
    joined = "; ".join(previews) if previews else "source memories retained for manual review"
    return f"[Derived] Compacted {len(memories)} approved {target_scope} memories: {joined}"


def select_compaction_sources(
    memories: list[dict[str, Any]],
    *,
    workspace_id: str,
    target_scope: str,
    requested_memory_ids: list[str] | None,
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    requested_list = [item for item in (requested_memory_ids or []) if item]
    requested_set = set(requested_list)
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    indexed_memories = {str(memory.get("id")): memory for memory in memories if memory.get("id")}
    iterable = memories if not requested_list else requested_list

    for item in iterable:
        memory = item if not requested_list else indexed_memories.get(str(item))
        if memory is None:
            skipped.append(
                {
                    "memory_id": str(item),
                    "identity": None,
                    "kind": None,
                    "reason": "requested_memory_not_found",
                }
            )
            continue
        metadata = memory.get("metadata", {}) or {}
        reason: str | None = None
        if metadata.get("workspace_id", workspace_id) != workspace_id or metadata.get("scope") != target_scope:
            reason = "workspace_scope_mismatch"
        elif memory.get("state") != "active":
            reason = "inactive_memory"
        elif not metadata.get("identity"):
            reason = "missing_identity"
        elif metadata.get("essential") is True:
            reason = "essential_memory"
        elif metadata.get("status") != "approved":
            reason = "status_not_approved"
        elif metadata.get("kind") not in ALLOWED_SOURCE_KINDS:
            reason = "unsupported_kind"

        if reason is not None:
            skipped.append(
                {
                    "memory_id": memory.get("id"),
                    "identity": metadata.get("identity"),
                    "kind": metadata.get("kind"),
                    "reason": reason,
                }
            )
            continue
        selected.append(memory)

    if not requested_list and limit > 0:
        selected = selected[:limit]

    skipped.sort(
        key=lambda item: (
            SKIPPED_REASON_PRIORITY.index(item["reason"])
            if item["reason"] in SKIPPED_REASON_PRIORITY
            else len(SKIPPED_REASON_PRIORITY),
            str(item.get("memory_id", "")),
        )
    )
    return selected, skipped


def build_compression_proposal(
    *,
    workspace_id: str,
    target_scope: str,
    selected_memories: list[dict[str, Any]],
    key: str | None = None,
) -> dict[str, Any]:
    source_memory_ids = [str(memory["id"]) for memory in selected_memories if memory.get("id")]
    source_identities = [
        str((memory.get("metadata", {}) or {}).get("identity"))
        for memory in selected_memories
        if (memory.get("metadata", {}) or {}).get("identity")
    ]
    raw_hashes = [build_memory_fingerprint(memory) for memory in selected_memories]
    source_evidence_hash = hashlib.sha256("|".join(raw_hashes).encode("utf-8")).hexdigest()
    evidence_id = "compaction-source-bundle"
    proposal_key = key or default_compaction_key(workspace_id, source_memory_ids)
    target_kind = "workspace" if target_scope == "workspace" else target_scope

    return {
        "goal": "compact routine approved memories without automatic writeback",
        "scope": target_scope,
        "allowed_tools": [
            "memory_search",
            "memory_read",
        ],
        "summary": f"Dry-run compaction proposal for {len(selected_memories)} approved {target_kind} memories.",
        "evidence": [
            {
                "id": evidence_id,
                "type": "memory",
                "value": f"{len(selected_memories)} approved memories selected for dry-run compaction",
                "ref": ",".join(source_memory_ids),
                "source": "approved_decision",
            }
        ],
        "state_delta": {
            "facts": [
                {
                    "key": proposal_key,
                    "value": build_compacted_value(selected_memories, target_scope=target_scope),
                    "scope": target_scope,
                    "source": "approved_decision",
                    "evidence_ids": [evidence_id],
                }
            ],
            "preferences": [],
            "decisions": [],
            "risks": [],
        },
        "risks": [
            "Derived summary may hide edge-case exceptions until manually approved.",
        ],
        "fallback_suggestion": "escalate_to_main_agent",
        "next_steps": [
            "Review compression manifest and retained evidence.",
            "Approve before any archive or writeback action.",
        ],
        "confidence": 0.82,
        "schema_version": "compression_proposal_v1",
        "proposal_kind": "compression_proposal",
        "compression_manifest": {
            "source_memory_ids": source_memory_ids,
            "source_identities": source_identities,
            "target_workspace_id": workspace_id,
            "target_scope": target_scope,
            "compression_summary": f"Compress {len(selected_memories)} approved memories into one derived fact.",
            "retained_risks": [],
            "retain_raw_audit_ids": [],
            "raw_audit_trail_hashes": raw_hashes,
            "source_evidence_hash": f"sha256:{source_evidence_hash}",
            "supersedes_ids": source_memory_ids,
            "archive_candidates": source_memory_ids,
            "defer_reason": "",
            "rollback_basis": "raw_audit_trail_hashes + source_memory_ids",
            "conflict_class": "none",
            "evidence_window": format_time_window(selected_memories),
            "risk_resolution_mode": "retain",
        },
    }


def build_compaction_dry_run(
    memories: list[dict[str, Any]],
    *,
    workspace_id: str,
    target_scope: str,
    requested_memory_ids: list[str] | None = None,
    limit: int = 4,
    key: str | None = None,
) -> dict[str, Any]:
    selected, skipped = select_compaction_sources(
        memories,
        workspace_id=workspace_id,
        target_scope=target_scope,
        requested_memory_ids=requested_memory_ids,
        limit=max(1, limit),
    )
    report: dict[str, Any] = {
        "ok": False,
        "mode": "dry-run",
        "workspace_id": workspace_id,
        "target_scope": target_scope,
        "requested_memory_ids": list(requested_memory_ids or []),
        "selection_report": {
            "selected_source_count": len(selected),
            "selected": [
                {
                    "memory_id": memory.get("id"),
                    "identity": (memory.get("metadata", {}) or {}).get("identity"),
                    "kind": (memory.get("metadata", {}) or {}).get("kind"),
                    "preview": render_source_preview(memory),
                }
                for memory in selected
            ],
            "skipped": skipped,
        },
    }
    if len(selected) < 2:
        report["defer_reason"] = "insufficient_source_memories"
        return report

    report["proposal"] = build_compression_proposal(
        workspace_id=workspace_id,
        target_scope=target_scope,
        selected_memories=selected,
        key=key,
    )
    report["ok"] = True
    return report
