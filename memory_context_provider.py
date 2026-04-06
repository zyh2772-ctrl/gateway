#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


ListMemoriesFn = Callable[[Any, str], list[dict[str, Any]]]
ExplainMemoryClassificationFn = Callable[[dict[str, Any], str], tuple[str | None, str, str | None]]
PassesHardFiltersFn = Callable[..., tuple[bool, str | None]]
GetEmbeddingInputTextFn = Callable[[dict[str, Any]], str]
ComputeSemanticSimilaritiesFn = Callable[..., tuple[dict[str, float], dict[str, Any], dict[str, dict[str, float]]]]
RankMemoryCandidateFn = Callable[..., dict[str, Any]]
PassesRetrievalSignalGateFn = Callable[..., tuple[bool, str]]
RenderCandidateTextFn = Callable[..., str]
MaybeTruncateCandidateTextFn = Callable[[str], str]
BlockLimitForFn = Callable[[Any, str], int]
RoleBudgetMultiplierForFn = Callable[[Any, str | None], float]
BlockPriorityForFn = Callable[[Any], list[str]]
EvaluateContextOverrideFn = Callable[..., dict[str, Any] | None]
EstimateItemTokensFn = Callable[[str], int]
ParseQueryTermsFn = Callable[[str], list[str]]
ExtractStrongEntitiesFn = Callable[[str], list[str]]
ResolveTriggerFn = Callable[..., str]


@dataclass(frozen=True)
class MemoryContextProviderDeps:
    list_memories: ListMemoriesFn
    explain_memory_classification: ExplainMemoryClassificationFn
    passes_hard_filters: PassesHardFiltersFn
    get_embedding_input_text: GetEmbeddingInputTextFn
    compute_semantic_similarities: ComputeSemanticSimilaritiesFn
    rank_memory_candidate: RankMemoryCandidateFn
    passes_retrieval_signal_gate: PassesRetrievalSignalGateFn
    render_candidate_text: RenderCandidateTextFn
    maybe_truncate_candidate_text: MaybeTruncateCandidateTextFn
    block_limit_for: BlockLimitForFn
    role_budget_multiplier_for: RoleBudgetMultiplierForFn
    block_priority_for: BlockPriorityForFn
    evaluate_context_override: EvaluateContextOverrideFn
    estimate_item_tokens: EstimateItemTokensFn
    parse_query_terms: ParseQueryTermsFn
    extract_strong_entities: ExtractStrongEntitiesFn
    resolve_trigger: ResolveTriggerFn


def trim_items(items: list[str], limit: int) -> list[str]:
    return items[:limit]


def select_role_blocks(
    blocks: dict[str, list[str]],
    *,
    role: str | None,
    role_allowed_blocks: dict[str, list[str]],
) -> dict[str, list[str]]:
    if role is None or role not in role_allowed_blocks:
        return {name: values for name, values in blocks.items() if values}
    allowed = set(role_allowed_blocks[role])
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


def estimate_tokens(blocks: dict[str, list[str]], query: str) -> int:
    text = render_context_text(blocks, workspace_id="estimate", query=query)
    return max(1, len(text) // 4)


def build_context_result(
    config: Any,
    deps: MemoryContextProviderDeps,
    *,
    user_id: str,
    workspace_id: str,
    role: str | None,
    query: str,
    trigger: str,
    context_override_blocks: set[str],
) -> dict[str, Any]:
    scoring = config.recall_scoring
    memories = deps.list_memories(config, user_id)
    blocks: dict[str, list[dict[str, Any]]] = {
        "Relevant Preferences": [],
        "Workspace Facts": [],
        "Prior Decisions": [],
        "Task Continuation State": [],
        "Known Risks": [],
        "Retrieved Facts": [],
    }
    debug_entries: list[dict[str, Any]] = []
    query_terms = deps.parse_query_terms(query)
    strong_entities = deps.extract_strong_entities(query)
    semantic_candidates: list[dict[str, Any]] = []

    for memory in memories:
        block_name, scope, classification_reason = deps.explain_memory_classification(memory, workspace_id)
        metadata = memory.get("metadata", {}) or {}
        debug_entry = {
            "memory_id": memory.get("id"),
            "kind": metadata.get("kind"),
            "scope": scope,
            "workspace_id": metadata.get("workspace_id"),
            "block_name": block_name,
            "identity": metadata.get("identity"),
            "preview": (memory.get("content") or "")[:200],
            "status": "pending",
        }
        debug_entries.append(debug_entry)
        if block_name is None:
            debug_entry["status"] = "filtered"
            debug_entry["reason"] = classification_reason or "classification_filtered"
            continue

        if metadata.get("state") in {"draft", "experimental"}:
            debug_entry["status"] = "filtered"
            debug_entry["reason"] = "draft_or_experimental"
            continue
        keep, reason = deps.passes_hard_filters(
            memory,
            block_name=block_name,
            scope=scope,
            workspace_id=workspace_id,
            trigger=trigger,
        )
        if not keep:
            debug_entry["status"] = "filtered"
            debug_entry["reason"] = reason or "hard_filter"
            continue

        semantic_candidates.append(
            {
                "candidate_id": memory.get("id") or metadata.get("identity") or f"candidate_{len(semantic_candidates)}",
                "embedding_text": deps.get_embedding_input_text(memory),
                "memory": memory,
                "debug_entry": debug_entry,
                "block_name": block_name,
            }
        )

    semantic_scores, semantic_debug, semantic_details = deps.compute_semantic_similarities(
        config,
        query=query,
        candidates=semantic_candidates,
    )

    for semantic_candidate in semantic_candidates:
        memory = semantic_candidate["memory"]
        debug_entry = semantic_candidate["debug_entry"]
        block_name = semantic_candidate["block_name"]
        metadata = memory.get("metadata", {}) or {}

        scores = deps.rank_memory_candidate(
            config,
            memory,
            block_name=block_name,
            role=role,
            query=query,
            trigger=trigger,
            semantic_score_override=semantic_scores.get(semantic_candidate["candidate_id"]),
        )
        if semantic_candidate["candidate_id"] in semantic_details:
            scores.update(semantic_details[semantic_candidate["candidate_id"]])
        debug_entry["scores"] = scores

        if scores["entity_alignment_score"] < scoring.entity_alignment_floor and strong_entities:
            debug_entry["status"] = "filtered"
            debug_entry["reason"] = "entity_alignment_zero"
            continue
        admitted, admit_reason = deps.passes_retrieval_signal_gate(
            config,
            scores,
            block_name=block_name,
            has_strong_entities=bool(strong_entities),
        )
        debug_entry["retrieval_gate"] = admit_reason
        if not admitted:
            debug_entry["status"] = "filtered"
            debug_entry["reason"] = admit_reason
            continue
        if block_name in {"Prior Decisions", "Relevant Preferences"}:
            if scores["approval_score"] < 0.75 or scores["low_confidence"]:
                debug_entry["status"] = "filtered"
                debug_entry["reason"] = "approval_or_low_confidence_gate"
                continue
        elif block_name == "Task Continuation State":
            if scores["trigger_fit_score"] < 0.60 and scores["final_score"] < scoring.final_score_min:
                debug_entry["status"] = "filtered"
                debug_entry["reason"] = "task_continuation_trigger_gate"
                continue
        elif scores["final_score"] < scoring.final_score_min:
            debug_entry["status"] = "filtered"
            debug_entry["reason"] = "block_threshold"
            continue

        text = deps.render_candidate_text(memory, low_confidence=scores["low_confidence"])
        text = deps.maybe_truncate_candidate_text(text)
        if not text:
            debug_entry["status"] = "filtered"
            debug_entry["reason"] = "empty_text"
            continue

        identity = metadata.get("identity") or f"{block_name}:{text[:120]}"
        debug_entry["identity"] = identity
        debug_entry["rendered_text"] = text
        debug_entry["status"] = "candidate"
        blocks[block_name].append(
            {
                "identity": identity,
                "text": text,
                "memory": memory,
                "scores": scores,
                "updated_at": memory.get("updated_at"),
                "debug_entry": debug_entry,
            }
        )

    rendered: dict[str, list[str]] = {}
    deduped_blocks: dict[str, list[dict[str, Any]]] = {name: [] for name in blocks}
    for name, candidates in blocks.items():
        best_by_identity: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            current = best_by_identity.get(candidate["identity"])
            if current is None:
                best_by_identity[candidate["identity"]] = candidate
                continue
            current_scores = current["scores"]
            candidate_scores = candidate["scores"]
            current_key = (
                current_scores["approval_score"],
                current_scores["recency_score"],
                current_scores["final_score"],
            )
            candidate_key = (
                candidate_scores["approval_score"],
                candidate_scores["recency_score"],
                candidate_scores["final_score"],
            )
            if candidate_key > current_key:
                current["debug_entry"]["status"] = "filtered"
                current["debug_entry"]["reason"] = "deduped_lower_score"
                best_by_identity[candidate["identity"]] = candidate
            else:
                candidate["debug_entry"]["status"] = "filtered"
                candidate["debug_entry"]["reason"] = "deduped_lower_score"
        ordered_candidates = sorted(
            best_by_identity.values(),
            key=lambda item: (
                item["scores"]["final_score"],
                item["scores"]["approval_score"],
                item["scores"]["recency_score"],
            ),
            reverse=True,
        )
        block_limit = deps.block_limit_for(config, name)
        for candidate in ordered_candidates[block_limit:]:
            candidate["debug_entry"]["status"] = "filtered"
            candidate["debug_entry"]["reason"] = "block_limit_trim"
        deduped_blocks[name] = ordered_candidates[:block_limit]

    stale_or_superseded: list[dict[str, Any]] = []
    for block_name in context_override_blocks:
        filtered_candidates: list[dict[str, Any]] = []
        for candidate in deduped_blocks[block_name]:
            override = deps.evaluate_context_override(
                candidate["memory"],
                block_name=block_name,
                query=query,
                scores=candidate["scores"],
                entity_alignment_floor=scoring.entity_alignment_floor,
            )
            if override is None:
                filtered_candidates.append(candidate)
                continue
            candidate["debug_entry"]["status"] = "filtered"
            candidate["debug_entry"]["reason"] = "context_override"
            candidate["debug_entry"]["context_override"] = override
            stale_or_superseded.append(
                {
                    "identity": candidate["identity"],
                    "block_name": block_name,
                    "reason": override["reason"],
                    "override_mode": override["override_mode"],
                    "approval_action": override["approval_action"],
                    "preview": candidate["text"][:200],
                }
            )
        deduped_blocks[block_name] = filtered_candidates

    role_multiplier = deps.role_budget_multiplier_for(config, role)
    total_budget = max(1, int(config.max_long_term_context_tokens * role_multiplier))
    used_tokens = 0
    selected_by_block: dict[str, list[str]] = {name: [] for name in blocks}
    for block_name in deps.block_priority_for(config):
        for candidate in deduped_blocks[block_name]:
            candidate_tokens = deps.estimate_item_tokens(candidate["text"])
            if used_tokens + candidate_tokens > total_budget and block_name != "Known Risks":
                candidate["debug_entry"]["status"] = "filtered"
                candidate["debug_entry"]["reason"] = "budget_trim"
                continue
            selected_by_block[block_name].append(candidate["text"])
            candidate["debug_entry"]["status"] = "selected"
            candidate["debug_entry"]["selected_block"] = block_name
            candidate["debug_entry"]["selected_tokens"] = candidate_tokens
            used_tokens += candidate_tokens

    for name, values in selected_by_block.items():
        trimmed = trim_items(values, deps.block_limit_for(config, name))
        if trimmed:
            rendered[name] = trimmed
    return {
        "rendered_blocks": rendered,
        "stale_or_superseded": stale_or_superseded,
        "debug": {
            "query_terms": query_terms,
            "strong_entities": strong_entities,
            "semantic_backend": semantic_debug,
            "thresholds": {
                "low_confidence_semantic_min": scoring.low_confidence_semantic_min,
                "mixed_semantic_min": scoring.mixed_semantic_min,
                "high_confidence_semantic_min": scoring.high_confidence_semantic_min,
                "low_confidence_lexical_hint": scoring.low_confidence_lexical_hint,
                "retrieved_facts_lexical_floor": scoring.retrieved_facts_lexical_floor,
                "retrieved_facts_low_confidence_semantic_min": scoring.retrieved_facts_low_confidence_semantic_min,
                "retrieved_facts_mixed_semantic_min": scoring.retrieved_facts_mixed_semantic_min,
                "final_score_min": scoring.final_score_min,
                "retrieved_facts_min": scoring.retrieved_facts_min,
                "lexical_floor": scoring.lexical_floor,
                "entity_alignment_floor": scoring.entity_alignment_floor,
                "weights": {
                    "lexical": scoring.lexical_weight,
                    "semantic": scoring.semantic_weight,
                    "approval": scoring.approval_weight,
                    "recency": scoring.recency_weight,
                    "role_fit": scoring.role_fit_weight,
                    "trigger_fit": scoring.trigger_fit_weight,
                    "entity_alignment": scoring.entity_alignment_weight,
                    "confidence": scoring.confidence_weight,
                },
            },
            "budget": {
                "role_multiplier": role_multiplier,
                "total_budget": total_budget,
                "used_tokens": used_tokens,
            },
            "limits": {
                "block_limits": {name: deps.block_limit_for(config, name) for name in blocks},
                "block_priority": deps.block_priority_for(config),
            },
            "per_memory": debug_entries,
        },
    }


def build_context_bundle(
    config: Any,
    deps: MemoryContextProviderDeps,
    *,
    user_id: str,
    workspace_id: str,
    role: str | None,
    query: str,
    trigger: str,
    context_override_blocks: set[str],
    role_allowed_blocks: dict[str, list[str]],
    debug: bool = False,
) -> dict[str, Any]:
    resolved_trigger = deps.resolve_trigger(
        config,
        user_id=user_id,
        workspace_id=workspace_id,
        trigger=trigger,
    )
    context_result = build_context_result(
        config,
        deps,
        user_id=user_id,
        workspace_id=workspace_id,
        role=role,
        query=query,
        trigger=resolved_trigger,
        context_override_blocks=context_override_blocks,
    )
    role_blocks = select_role_blocks(
        context_result["rendered_blocks"],
        role=role,
        role_allowed_blocks=role_allowed_blocks,
    )
    role_multiplier = deps.role_budget_multiplier_for(config, role)
    payload = {
        "role": role,
        "trigger": resolved_trigger,
        "user_id": user_id,
        "workspace_id": workspace_id,
        "query": query,
        "context_blocks": role_blocks,
        "source_context_blocks": list(role_blocks.keys()),
        "stale_or_superseded": context_result.get("stale_or_superseded", []),
        "context_text": render_context_text(role_blocks, workspace_id=workspace_id, query=query),
        "context_token_estimate": estimate_tokens(role_blocks, query),
        "budget_profile": {
            "role_multiplier": role_multiplier,
            "model_multiplier": 1.0,
            "max_tokens": max(1, int(config.max_long_term_context_tokens * role_multiplier)),
        },
    }
    if debug:
        payload["debug"] = context_result["debug"]
    return payload
