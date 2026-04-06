#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import math
import os
import re
import site
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from main_agent_middleware import (
    RuntimeMiddlewareDeps,
    run_approve_middleware,
    run_finalize_middleware,
    run_recall_middleware,
)
from memory_compactor import build_compaction_dry_run
from memory_context_provider import (
    MemoryContextProviderDeps,
    build_context_bundle as provider_build_context_bundle,
    build_context_result as provider_build_context_result,
    estimate_tokens as provider_estimate_tokens,
    render_context_text as provider_render_context_text,
    select_role_blocks as provider_select_role_blocks,
)

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


ROOT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = ROOT_DIR / "main-agent-runtime.toml"
VALIDATOR_PATH = ROOT_DIR.parent / "codex-global-multi-agent" / "scripts" / "validate_and_merge.py"
LOCAL_BGE_M3_HELPER_PATH = ROOT_DIR / "semantic_local_bge_helper.py"

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
    "Compactor": [
        "Workspace Facts",
        "Prior Decisions",
        "Known Risks",
        "Retrieved Facts",
    ],
}

ROLE_FOCUS_BLOCKS = {
    "Planner": {
        "Relevant Preferences",
        "Workspace Facts",
        "Prior Decisions",
        "Task Continuation State",
        "Known Risks",
    },
    "Retriever": {
        "Workspace Facts",
        "Retrieved Facts",
    },
    "Verifier": {
        "Prior Decisions",
        "Known Risks",
        "Retrieved Facts",
    },
    "Implementer": {
        "Task Continuation State",
        "Prior Decisions",
    },
    "Synthesizer": set(ROLE_ALLOWED_BLOCKS["Synthesizer"]),
    "Compactor": set(ROLE_ALLOWED_BLOCKS["Compactor"]),
}

DEFAULT_ROLE_BUDGET_MULTIPLIERS = {
    "Planner": 1.1,
    "Verifier": 0.9,
    "Implementer": 0.45,
    "Retriever": 0.5,
    "Synthesizer": 1.0,
    "Compactor": 0.75,
}

DEFAULT_BLOCK_LIMITS = {
    "Relevant Preferences": 3,
    "Workspace Facts": 4,
    "Prior Decisions": 4,
    "Task Continuation State": 3,
    "Known Risks": 3,
    "Retrieved Facts": 2,
}

DEFAULT_BLOCK_PRIORITY = [
    "Known Risks",
    "Task Continuation State",
    "Prior Decisions",
    "Workspace Facts",
    "Relevant Preferences",
    "Retrieved Facts",
]

DEFAULT_LOW_CONFIDENCE_SEMANTIC_MIN = 0.58
DEFAULT_MIXED_SEMANTIC_MIN = 0.70
DEFAULT_HIGH_CONFIDENCE_SEMANTIC_MIN = 0.82
DEFAULT_FINAL_SCORE_MIN = 0.45
DEFAULT_RETRIEVED_FACTS_MIN = 0.35
DEFAULT_LEXICAL_FLOOR = 0.20
DEFAULT_LOW_CONFIDENCE_LEXICAL_HINT = 0.05
DEFAULT_RETRIEVED_FACTS_LEXICAL_FLOOR = 0.22
DEFAULT_RETRIEVED_FACTS_LOW_CONFIDENCE_SEMANTIC_MIN = 0.64
DEFAULT_RETRIEVED_FACTS_MIXED_SEMANTIC_MIN = 0.78
LIFECYCLE_STAGE_BY_TRIGGER = {
    "before_task_start": "start",
    "on_workspace_switch": "start",
    "after_failure": "active",
    "before_subagent_spawn": "active",
    "auto": "active",
    "finalize": "finalize",
}
CONFIDENCE_BACKFILL_ALLOWED_KINDS = {
    "facts",
    "preferences",
    "decisions",
    "risks",
    "retrieved_fact",
    "retrieved_facts",
    "compressed_fact",
    "derived_fact",
}
CONFIDENCE_BACKFILL_BLOCKED_KINDS = {
    "audit_record",
    "task_state",
    "task_summary",
    "fallback_history",
}
CONFIDENCE_BACKFILL_BLOCKED_METADATA_STATES = {
    "archived",
    "deleted",
    "outdated",
    "superseded",
}
DEFAULT_ENTITY_ALIGNMENT_FLOOR = 0.50
MAX_ITEM_TOKENS = 220
DEFAULT_SEMANTIC_API_KEY = "sk-local-gateway"
CJK_CHAR_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
DEFAULT_LOCAL_BGE_M3_MODEL_PATH = Path("/Users/zyh/.lmstudio/models/BAAI/bge-m3")
LOCAL_BGE_M3_MODEL: Any | None = None
LOCAL_BGE_M3_LOAD_ERROR: str | None = None
CONTEXT_OVERRIDE_BLOCKS = {"Relevant Preferences", "Prior Decisions"}
CONTEXT_OVERRIDE_NEGATION_MARKERS = (
    "不要",
    "不再",
    "别再",
    "停止",
    "禁用",
    "关闭",
    "取消",
    "移除",
    "去掉",
    "不用",
    "别用",
)
CONTEXT_OVERRIDE_REPLACEMENT_MARKERS = (
    "改为",
    "改成",
    "换成",
    "改用",
    "切换到",
)
CONTEXT_OVERRIDE_NEGATIVE_POLARITY_MARKERS = (
    "不要",
    "不再",
    "不能",
    "禁止",
    "避免",
    "关闭",
    "取消",
    "移除",
    "去掉",
    "不用",
    "别用",
    "不写入",
    "不进入",
    "不升级",
    "不使用",
)
CONTEXT_OVERRIDE_POSITIVE_POLARITY_MARKERS = (
    "使用",
    "启用",
    "开启",
    "写入",
    "进入",
    "升级",
    "保留",
    "采用",
    "允许",
    "偏好",
    "喜欢",
)
CONTEXT_OVERRIDE_MIN_LEXICAL_SCORE = 0.16


@dataclass
class RecallScoringConfig:
    lexical_floor: float
    retrieved_facts_lexical_floor: float
    low_confidence_lexical_hint: float
    low_confidence_semantic_min: float
    mixed_semantic_min: float
    high_confidence_semantic_min: float
    final_score_min: float
    retrieved_facts_min: float
    retrieved_facts_low_confidence_semantic_min: float
    retrieved_facts_mixed_semantic_min: float
    entity_alignment_floor: float
    lexical_weight: float
    semantic_weight: float
    approval_weight: float
    recency_weight: float
    role_fit_weight: float
    trigger_fit_weight: float
    entity_alignment_weight: float
    confidence_weight: float


@dataclass
class RecallLimitsConfig:
    role_budget_multipliers: dict[str, float]
    block_limits: dict[str, int]
    block_priority: list[str]


@dataclass
class RuntimeConfig:
    default_user_id: str
    default_workspace_id: str
    default_app: str
    sqlite_db: Path
    compat_helper: Path
    max_memories: int
    max_stage_context_tokens: int
    max_long_term_context_tokens: int
    semantic_enabled: bool
    semantic_backend: str
    semantic_api_base: str
    semantic_model: str
    semantic_api_key_env: str
    semantic_timeout_seconds: float
    semantic_batch_size: int
    semantic_local_model_path: Path
    semantic_local_cache_dir: Path
    semantic_local_use_fp16: bool
    semantic_local_dense_weight: float
    semantic_local_sparse_weight: float
    semantic_local_max_query_length: int
    semantic_local_max_passage_length: int
    recall_scoring: RecallScoringConfig
    recall_limits: RecallLimitsConfig
    writeback_enabled: bool
    store_audit_records: bool
    state_path: Path


def _get_float(raw: dict[str, Any], key: str, default: float) -> float:
    value = raw.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _get_int(raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def load_recall_scoring_config(raw: dict[str, Any]) -> RecallScoringConfig:
    return RecallScoringConfig(
        lexical_floor=_get_float(raw, "lexical_floor", DEFAULT_LEXICAL_FLOOR),
        retrieved_facts_lexical_floor=_get_float(
            raw, "retrieved_facts_lexical_floor", DEFAULT_RETRIEVED_FACTS_LEXICAL_FLOOR
        ),
        low_confidence_lexical_hint=_get_float(
            raw, "low_confidence_lexical_hint", DEFAULT_LOW_CONFIDENCE_LEXICAL_HINT
        ),
        low_confidence_semantic_min=_get_float(
            raw, "low_confidence_semantic_min", DEFAULT_LOW_CONFIDENCE_SEMANTIC_MIN
        ),
        mixed_semantic_min=_get_float(raw, "mixed_semantic_min", DEFAULT_MIXED_SEMANTIC_MIN),
        high_confidence_semantic_min=_get_float(
            raw, "high_confidence_semantic_min", DEFAULT_HIGH_CONFIDENCE_SEMANTIC_MIN
        ),
        final_score_min=_get_float(raw, "final_score_min", DEFAULT_FINAL_SCORE_MIN),
        retrieved_facts_min=_get_float(raw, "retrieved_facts_min", DEFAULT_RETRIEVED_FACTS_MIN),
        retrieved_facts_low_confidence_semantic_min=_get_float(
            raw,
            "retrieved_facts_low_confidence_semantic_min",
            DEFAULT_RETRIEVED_FACTS_LOW_CONFIDENCE_SEMANTIC_MIN,
        ),
        retrieved_facts_mixed_semantic_min=_get_float(
            raw,
            "retrieved_facts_mixed_semantic_min",
            DEFAULT_RETRIEVED_FACTS_MIXED_SEMANTIC_MIN,
        ),
        entity_alignment_floor=_get_float(
            raw, "entity_alignment_floor", DEFAULT_ENTITY_ALIGNMENT_FLOOR
        ),
        lexical_weight=_get_float(raw, "lexical_weight", 0.35),
        semantic_weight=_get_float(raw, "semantic_weight", 0.20),
        approval_weight=_get_float(raw, "approval_weight", 0.15),
        recency_weight=_get_float(raw, "recency_weight", 0.10),
        role_fit_weight=_get_float(raw, "role_fit_weight", 0.08),
        trigger_fit_weight=_get_float(raw, "trigger_fit_weight", 0.05),
        entity_alignment_weight=_get_float(raw, "entity_alignment_weight", 0.04),
        confidence_weight=_get_float(raw, "confidence_weight", 0.03),
    )


def load_recall_limits_config(raw: dict[str, Any]) -> RecallLimitsConfig:
    raw_role_multipliers = raw.get("role_budget_multipliers", {})
    role_budget_multipliers = dict(DEFAULT_ROLE_BUDGET_MULTIPLIERS)
    if isinstance(raw_role_multipliers, dict):
        for key, value in raw_role_multipliers.items():
            try:
                role_budget_multipliers[str(key)] = float(value)
            except (TypeError, ValueError):
                continue

    raw_block_limits = raw.get("block_limits", {})
    block_limits = dict(DEFAULT_BLOCK_LIMITS)
    if isinstance(raw_block_limits, dict):
        for key, value in raw_block_limits.items():
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed >= 0:
                block_limits[str(key)] = parsed

    raw_block_priority = raw.get("block_priority", DEFAULT_BLOCK_PRIORITY)
    if isinstance(raw_block_priority, list):
        block_priority = [str(item) for item in raw_block_priority if isinstance(item, str)]
    else:
        block_priority = list(DEFAULT_BLOCK_PRIORITY)
    if not block_priority:
        block_priority = list(DEFAULT_BLOCK_PRIORITY)

    return RecallLimitsConfig(
        role_budget_multipliers=role_budget_multipliers,
        block_limits=block_limits,
        block_priority=block_priority,
    )


def load_config(path: Path) -> RuntimeConfig:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    memory = raw["memory"]
    budget = raw["budget"]
    semantic = raw.get("semantic_recall", {})
    recall_scoring = load_recall_scoring_config(raw.get("recall_scoring", {}))
    recall_limits = load_recall_limits_config(raw.get("recall_limits", {}))
    writeback = raw.get("writeback", {})
    state = raw.get("state", {})
    state_path = (path.parent / state.get("path", "./runtime/main-agent-runtime-state.json")).resolve()
    local_model_path_raw = semantic.get("local_model_path")
    if local_model_path_raw:
        local_model_path = Path(str(local_model_path_raw)).expanduser()
        if not local_model_path.is_absolute():
            local_model_path = (path.parent / local_model_path).resolve()
    else:
        local_model_path = DEFAULT_LOCAL_BGE_M3_MODEL_PATH
    local_cache_dir = Path(str(semantic.get("local_cache_dir", "./runtime/huggingface-cache"))).expanduser()
    if not local_cache_dir.is_absolute():
        local_cache_dir = (path.parent / local_cache_dir).resolve()

    return RuntimeConfig(
        default_user_id=memory.get("default_user_id", "zyh"),
        default_workspace_id=memory.get("default_workspace_id", "default-workspace"),
        default_app=memory.get("default_app", "openmemory"),
        sqlite_db=(path.parent / memory["sqlite_db"]).resolve(),
        compat_helper=(path.parent / memory["compat_helper"]).resolve(),
        max_memories=int(memory.get("max_memories", 50)),
        max_stage_context_tokens=int(budget.get("max_stage_context_tokens", 1200)),
        max_long_term_context_tokens=int(budget.get("max_long_term_context_tokens", 1200)),
        semantic_enabled=bool(semantic.get("enabled", True)),
        semantic_backend=str(semantic.get("backend", "openai_api")).strip() or "openai_api",
        semantic_api_base=str(semantic.get("api_base", "http://127.0.0.1:4000/v1")).rstrip("/"),
        semantic_model=str(semantic.get("model", "embed-m3")),
        semantic_api_key_env=str(semantic.get("api_key_env", "LITELLM_MASTER_KEY")),
        semantic_timeout_seconds=float(semantic.get("timeout_seconds", 20)),
        semantic_batch_size=max(1, int(semantic.get("batch_size", 16))),
        semantic_local_model_path=local_model_path,
        semantic_local_cache_dir=local_cache_dir,
        semantic_local_use_fp16=bool(semantic.get("local_use_fp16", False)),
        semantic_local_dense_weight=max(0.0, float(semantic.get("local_dense_weight", 0.7))),
        semantic_local_sparse_weight=max(0.0, float(semantic.get("local_sparse_weight", 0.3))),
        semantic_local_max_query_length=max(8, int(semantic.get("local_max_query_length", 256))),
        semantic_local_max_passage_length=max(32, int(semantic.get("local_max_passage_length", 512))),
        recall_scoring=recall_scoring,
        recall_limits=recall_limits,
        writeback_enabled=bool(writeback.get("enabled", True)),
        store_audit_records=bool(writeback.get("store_audit_records", True)),
        state_path=state_path,
    )


def load_validator() -> Any:
    spec = importlib.util.spec_from_file_location("validate_and_merge", VALIDATOR_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError("failed to load validator module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = load_validator()


def ensure_state_path(config: RuntimeConfig) -> None:
    try:
        config.state_path.parent.mkdir(parents=True, exist_ok=True)
        if not config.state_path.exists():
            config.state_path.write_text('{"workspaces": {}, "__meta": {"last_workspace_by_user": {}}}\n', encoding="utf-8")
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "codex-main-agent-runtime-state.json"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        if not fallback.exists():
            fallback.write_text('{"workspaces": {}, "__meta": {"last_workspace_by_user": {}}}\n', encoding="utf-8")
        config.state_path = fallback


def load_state(config: RuntimeConfig) -> dict[str, Any]:
    ensure_state_path(config)
    try:
        raw = json.loads(config.state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raw = {}
    if "workspaces" in raw and "__meta" in raw:
        return raw
    return {
        "workspaces": raw if isinstance(raw, dict) else {},
        "__meta": {"last_workspace_by_user": {}},
    }


def save_state(config: RuntimeConfig, state: dict[str, Any]) -> None:
    ensure_state_path(config)
    config.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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


def sqlite_connection(config: RuntimeConfig) -> sqlite3.Connection:
    connection = sqlite3.connect(str(config.sqlite_db))
    connection.row_factory = sqlite3.Row
    return connection


def sqlite_list_memories(config: RuntimeConfig, user_id: str) -> list[dict[str, Any]]:
    with sqlite_connection(config) as connection:
        rows = connection.execute(
            """
            SELECT m.id, u.user_id AS user_name, a.name AS app_name, m.content, m.metadata, m.state, m.created_at, m.updated_at
            FROM memories m
            JOIN users u ON m.user_id = u.id
            JOIN apps a ON m.app_id = a.id
            WHERE u.user_id = ?
            ORDER BY m.created_at DESC
            LIMIT ?
            """,
            (user_id, config.max_memories),
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["metadata"] = json.loads(item["metadata"]) if item["metadata"] else {}
        items.append(item)
    return items


def sqlite_list_all_memories(config: RuntimeConfig, user_id: str) -> list[dict[str, Any]]:
    with sqlite_connection(config) as connection:
        rows = connection.execute(
            """
            SELECT m.id, u.user_id AS user_name, a.name AS app_name, m.content, m.metadata, m.state, m.created_at, m.updated_at
            FROM memories m
            JOIN users u ON m.user_id = u.id
            JOIN apps a ON m.app_id = a.id
            WHERE u.user_id = ?
            ORDER BY m.created_at DESC
            """,
            (user_id,),
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["metadata"] = json.loads(item["metadata"]) if item["metadata"] else {}
        items.append(item)
    return items


def sqlite_find_user_and_app(connection: sqlite3.Connection, user_id: str, app_name: str) -> tuple[sqlite3.Row, sqlite3.Row]:
    user = connection.execute(
        "SELECT id, user_id FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if not user:
        raise RuntimeError("user_not_found")
    app = connection.execute(
        "SELECT id, name FROM apps WHERE owner_id = ? AND name = ?",
        (user["id"], app_name),
    ).fetchone()
    if not app:
        raise RuntimeError("app_not_found")
    return user, app


def sqlite_add_memory(config: RuntimeConfig, user_id: str, content: str, metadata: dict[str, Any]) -> dict[str, Any]:
    now = datetime.datetime.now(datetime.UTC).isoformat(sep=" ")
    memory_id = uuid.uuid4().hex
    with sqlite_connection(config) as connection:
        user, app = sqlite_find_user_and_app(connection, user_id, config.default_app)
        connection.execute(
            """
            INSERT INTO memories
            (id, user_id, app_id, content, vector, metadata, state, created_at, updated_at, archived_at, deleted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                user["id"],
                app["id"],
                content,
                None,
                json.dumps(metadata, ensure_ascii=False),
                "active",
                now,
                now,
                None,
                None,
            ),
        )
        connection.commit()
    return {
        "id": memory_id,
        "user_id": user["user_id"],
        "app_name": app["name"],
        "content": content,
        "state": "active",
    }


def sqlite_update_memory(
    config: RuntimeConfig,
    memory_id: str,
    *,
    content: str | None = None,
    state: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = datetime.datetime.now(datetime.UTC).isoformat(sep=" ")
    with sqlite_connection(config) as connection:
        row = connection.execute(
            "SELECT id, content, metadata, state FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        if not row:
            raise RuntimeError("memory_not_found")
        new_content = content if content is not None else row["content"]
        existing_metadata = json.loads(row["metadata"]) if row["metadata"] else {}
        new_metadata = metadata if metadata is not None else existing_metadata
        new_state = state if state is not None else row["state"]
        archived_at = now if new_state == "archived" else None
        deleted_at = now if new_state == "deleted" else None
        connection.execute(
            """
            UPDATE memories
            SET content = ?, metadata = ?, state = ?, updated_at = ?, archived_at = ?, deleted_at = ?
            WHERE id = ?
            """,
            (
                new_content,
                json.dumps(new_metadata, ensure_ascii=False),
                new_state,
                now,
                archived_at,
                deleted_at,
                memory_id,
            ),
        )
        connection.commit()
        updated = connection.execute(
            "SELECT id, content, metadata, state, updated_at FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
    result = dict(updated)
    result["metadata"] = json.loads(result["metadata"]) if result.get("metadata") else {}
    return result


def list_memories(config: RuntimeConfig, user_id: str) -> list[dict[str, Any]]:
    if config.sqlite_db.exists():
        return sqlite_list_memories(config, user_id)
    payload = run_compat_command(config.compat_helper, ["list", "--user", user_id])
    return payload[: config.max_memories] if isinstance(payload, list) else []


def add_memory(config: RuntimeConfig, user_id: str, content: str, metadata: dict[str, Any]) -> dict[str, Any]:
    if config.sqlite_db.exists():
        return sqlite_add_memory(config, user_id, content, metadata)
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


def update_memory(
    config: RuntimeConfig,
    memory_id: str,
    *,
    content: str | None = None,
    state: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if config.sqlite_db.exists():
        return sqlite_update_memory(config, memory_id, content=content, state=state, metadata=metadata)
    command = ["update", "--id", memory_id]
    if content is not None:
        command.extend(["--content", content])
    if state is not None:
        command.extend(["--state", state])
    if metadata is not None:
        command.extend(["--metadata", json.dumps(metadata, ensure_ascii=False)])
    return run_compat_command(config.compat_helper, command)


def list_all_memories_for_backfill(config: RuntimeConfig, user_id: str) -> list[dict[str, Any]]:
    if not config.sqlite_db.exists():
        raise RuntimeError("confidence_backfill_requires_sqlite_backend")
    return sqlite_list_all_memories(config, user_id)


def resolve_backfill_confidence_score(memory: dict[str, Any]) -> tuple[float | None, str]:
    metadata = memory.get("metadata", {}) or {}
    explicit = metadata.get("confidence_score")
    if isinstance(explicit, (int, float)):
        return None, "has_explicit_confidence"

    if memory.get("state") in {"archived", "deleted"}:
        return None, "memory_state_filtered"

    metadata_state = metadata.get("state")
    if metadata_state in CONFIDENCE_BACKFILL_BLOCKED_METADATA_STATES:
        return None, "metadata_state_filtered"

    kind = metadata.get("kind")
    if kind in CONFIDENCE_BACKFILL_BLOCKED_KINDS:
        return None, "blocked_kind"
    if kind not in CONFIDENCE_BACKFILL_ALLOWED_KINDS:
        return None, "unsupported_kind"

    if metadata.get("status") != "approved" and not metadata.get("approved_by"):
        return None, "unapproved_memory"

    evidence_ids = metadata.get("evidence_ids", [])
    has_evidence = isinstance(evidence_ids, list) and bool(evidence_ids)
    source = metadata.get("source")
    compression_manifest = metadata.get("compression_manifest")
    if kind in {"compressed_fact", "derived_fact"} or source == "compression-derived" or isinstance(
        compression_manifest, dict
    ):
        return 0.8, "compression-derived"
    if source == "user_claim":
        return 1.0, "user_claim"
    if source == "approved_decision":
        return 0.9, "approved_decision"
    if source in {"observed_fact", "tool_output"}:
        return (0.9 if has_evidence else 0.82), source
    if source == "model_inference":
        return 0.35, "model_inference"
    return None, "unsupported_source"


def backfill_confidence_scores(
    config: RuntimeConfig,
    *,
    user_id: str,
    workspace_id: str | None,
    ids: list[str] | None,
    limit: int | None,
    mode: str,
) -> dict[str, Any]:
    existing = list_all_memories_for_backfill(config, user_id)
    requested_ids = {item for item in (ids or []) if item}
    updates: list[dict[str, Any]] = []
    skip_reasons: dict[str, int] = {}
    scanned_count = 0

    for memory in existing:
        metadata = memory.get("metadata", {}) or {}
        if requested_ids and str(memory.get("id")) not in requested_ids:
            continue
        item_workspace_id = metadata.get("workspace_id")
        if workspace_id and item_workspace_id != workspace_id:
            continue
        scanned_count += 1

        score, rule = resolve_backfill_confidence_score(memory)
        if score is None:
            skip_reasons[rule] = skip_reasons.get(rule, 0) + 1
            continue

        runtime_estimate = compute_confidence_score(config, memory, 0.0)
        new_metadata = dict(metadata)
        new_metadata["confidence_score"] = score
        new_metadata["confidence_backfilled_at"] = int(time.time())
        new_metadata["confidence_backfilled_by"] = "main_agent_runtime"
        new_metadata["confidence_backfill_rule"] = rule
        updates.append(
            {
                "id": str(memory.get("id")),
                "identity": metadata.get("identity"),
                "workspace_id": item_workspace_id,
                "state": memory.get("state"),
                "kind": metadata.get("kind"),
                "source": metadata.get("source"),
                "rule": rule,
                "runtime_estimate_before": runtime_estimate,
                "confidence_score_before": metadata.get("confidence_score"),
                "confidence_score_after": score,
                "delta_vs_runtime_estimate": round(score - runtime_estimate, 6),
                "preview": (memory.get("content") or "")[:160],
                "metadata_before": metadata,
                "metadata_after": new_metadata,
                "action": "would_update" if mode == "dry-run" else "pending_update",
            }
        )
        if limit is not None and limit > 0 and len(updates) >= limit:
            break

    applied_updates: list[dict[str, Any]] = []
    if mode == "apply":
        for item in updates:
            updated = update_memory(config, item["id"], metadata=item["metadata_after"])
            item["action"] = "updated"
            item["updated_at"] = updated.get("updated_at")
            applied_updates.append(item)

    return {
        "ok": True,
        "mode": mode,
        "user_id": user_id,
        "workspace_id": workspace_id,
        "requested_ids": sorted(requested_ids) if requested_ids else [],
        "requested_ids_count": len(requested_ids),
        "scanned_count": scanned_count,
        "eligible_count": len(updates),
        "updated_count": len(applied_updates),
        "limit": limit,
        "skip_reasons": skip_reasons,
        "updates": applied_updates if mode == "apply" else updates,
    }


def finalize_task_state(
    config: RuntimeConfig,
    *,
    user_id: str,
    workspace_id: str,
    query: str,
) -> dict[str, Any]:
    existing = list_memories(config, user_id)
    finalized: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    finalized_at = int(time.time())

    for memory in existing:
        metadata = memory.get("metadata", {}) or {}
        block_name, scope, reason = explain_memory_classification(memory, workspace_id)
        kind = metadata.get("kind")
        if reason == "workspace_scope_mismatch":
            continue
        if kind == "audit_record":
            continue
        if block_name != "Task Continuation State" or scope != "task":
            continue
        if memory.get("state") != "active":
            continue
        identity = metadata.get("identity") or memory.get("id")
        if metadata.get("essential") is True or metadata.get("retain_after_finalize") is True:
            skipped.append(
                {
                    "memory_id": memory.get("id"),
                    "identity": identity,
                    "kind": kind,
                    "action": "retained",
                    "reason": "essential_or_explicit_retention",
                }
            )
            continue
        if kind not in {"task_state", "fallback_history"}:
            skipped.append(
                {
                    "memory_id": memory.get("id"),
                    "identity": identity,
                    "kind": kind,
                    "action": "retained",
                    "reason": "non_finalize_kind",
                }
            )
            continue

        new_metadata = dict(metadata)
        new_metadata["state"] = "outdated"
        new_metadata["lifecycle"] = "stale"
        new_metadata["finalized_at"] = finalized_at
        new_metadata["finalized_by"] = "main_agent_runtime"
        new_metadata["finalize_reason"] = query[:220] if query else "task_finalized"
        updated = update_memory(config, str(memory["id"]), metadata=new_metadata)
        finalized.append(
            {
                "memory_id": memory.get("id"),
                "identity": identity,
                "kind": kind,
                "action": "marked_outdated",
                "metadata_state": updated.get("metadata", {}).get("state"),
            }
        )

    state = load_state(config)
    workspaces = state.setdefault("workspaces", {})
    key = build_state_key(user_id, workspace_id)
    bucket = workspaces.get(
        key,
        {
            "user_id": user_id,
            "workspace_id": workspace_id,
            "lifecycle_stage": "finalize",
            "active_roles": [],
            "fallback_history": [],
            "memory_budget": {},
            "task_summary": "",
        },
    )
    last_context_blocks = bucket.get("last_context_blocks", {})
    if isinstance(last_context_blocks, dict):
        retained_context_blocks = {name: values for name, values in last_context_blocks.items() if name != "Task Continuation State"}
    else:
        retained_context_blocks = {}
    bucket["active_roles"] = []
    bucket["fallback_history"] = []
    bucket["memory_budget"] = {}
    bucket["last_trigger"] = "finalize"
    bucket["lifecycle_stage"] = "finalize"
    bucket["last_query"] = query[:220]
    bucket["last_context_blocks"] = retained_context_blocks
    bucket["updated_at"] = finalized_at
    bucket["run_revision"] = int(bucket.get("run_revision", 0)) + 1
    bucket["context_snapshot_id"] = uuid.uuid4().hex
    bucket["last_finalized_at"] = finalized_at
    bucket["finalized_task_continuation_count"] = len(finalized)
    workspaces[key] = bucket
    meta = state.setdefault("__meta", {})
    meta.setdefault("last_workspace_by_user", {})[user_id] = workspace_id
    meta["updated_at"] = finalized_at
    save_state(config, state)

    return {
        "ok": True,
        "user_id": user_id,
        "workspace_id": workspace_id,
        "query": query,
        "finalized_at": finalized_at,
        "finalized_task_memories": finalized,
        "skipped_task_memories": skipped,
        "finalized_count": len(finalized),
        "retained_count": len(skipped),
        "run_state": normalize_run_state_bucket(bucket),
    }


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


def explain_memory_classification(memory: dict[str, Any], workspace_id: str) -> tuple[str | None, str, str | None]:
    metadata = memory.get("metadata", {}) or {}
    scope = metadata.get("scope", "task")
    item_workspace = metadata.get("workspace_id")
    if scope == "workspace" and item_workspace not in {None, workspace_id}:
        return None, scope, "workspace_scope_mismatch"
    if scope == "task" and item_workspace not in {None, workspace_id}:
        return None, scope, "task_scope_mismatch"

    kind = metadata.get("kind")
    if kind == "audit_record":
        return None, scope, "audit_record_excluded"
    if kind == "preferences":
        return "Relevant Preferences", scope, None
    if kind == "facts" and scope == "workspace":
        return "Workspace Facts", scope, None
    if kind == "decisions":
        return "Prior Decisions", scope, None
    if kind == "risks":
        return "Known Risks", scope, None
    if kind in {"retrieved_facts", "retrieved_fact"}:
        return "Retrieved Facts", scope, None
    if kind in {"task_summary", "task_state", "fallback_history"} or scope == "task":
        return "Task Continuation State", scope, None
    return None, scope, "kind_not_mapped"


def classify_memory_item(memory: dict[str, Any], workspace_id: str) -> tuple[str | None, str]:
    block_name, scope, _ = explain_memory_classification(memory, workspace_id)
    return block_name, scope


def clamp_score(value: float) -> float:
    return max(0.0, min(1.0, value))


def normalize_match_text(text: str) -> str:
    return re.sub(r"[\s`'\".,;:()\[\]{}]+", "", text.lower())


def extract_cjk_terms(query: str) -> list[str]:
    compact = normalize_match_text(query)
    segments = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,}", compact)
    terms: list[str] = []
    for segment in segments:
        if 2 <= len(segment) <= 8:
            terms.append(segment)
        for size in (3, 2):
            if len(segment) < size:
                continue
            for index in range(len(segment) - size + 1):
                terms.append(segment[index : index + size])
    return list(dict.fromkeys(terms))


def parse_query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for raw in re.split(r"\s+", query.lower()):
        token = raw.strip("`'\".,;:()[]{}")
        if len(token) >= 2:
            terms.append(token)
    if CJK_CHAR_RE.search(query):
        terms.extend(extract_cjk_terms(query))
    seen: set[str] = set()
    unique_terms: list[str] = []
    for term in terms:
        if term not in seen:
            seen.add(term)
            unique_terms.append(term)
    return unique_terms[:12]


def extract_strong_entities(query: str) -> list[str]:
    entities: list[str] = []
    for raw in re.split(r"\s+", query):
        token = raw.strip("`'\".,;:()[]{}")
        if len(token) < 2:
            continue
        if any(ch in token for ch in "/._-") or any(ch.isdigit() for ch in token):
            entities.append(token.lower())
    return list(dict.fromkeys(entities))


def normalized_contains_any(text: str, markers: tuple[str, ...]) -> bool:
    normalized = normalize_match_text(text)
    return any(normalize_match_text(marker) in normalized for marker in markers)


def detect_context_override_mode(query: str) -> str | None:
    if normalized_contains_any(query, CONTEXT_OVERRIDE_REPLACEMENT_MARKERS):
        return "replace"
    if normalized_contains_any(query, CONTEXT_OVERRIDE_NEGATION_MARKERS):
        return "negate"
    return None


def classify_context_override_polarity(text: str) -> str | None:
    normalized = normalize_match_text(text)
    negative_hits = sum(
        1
        for marker in CONTEXT_OVERRIDE_NEGATIVE_POLARITY_MARKERS
        if normalize_match_text(marker) in normalized
    )
    positive_hits = sum(
        1
        for marker in CONTEXT_OVERRIDE_POSITIVE_POLARITY_MARKERS
        if normalize_match_text(marker) in normalized
    )
    if negative_hits > 0 and negative_hits >= positive_hits:
        return "negative"
    if positive_hits > 0:
        return "positive"
    return None


def evaluate_context_override(
    memory: dict[str, Any],
    *,
    block_name: str,
    query: str,
    scores: dict[str, Any],
    entity_alignment_floor: float,
) -> dict[str, Any] | None:
    if block_name not in CONTEXT_OVERRIDE_BLOCKS:
        return None
    mode = detect_context_override_mode(query)
    if mode is None:
        return None
    if (
        scores["lexical_score"] < CONTEXT_OVERRIDE_MIN_LEXICAL_SCORE
        and scores["entity_alignment_score"] < entity_alignment_floor
    ):
        return None

    content = (memory.get("content") or "").strip()
    if not content:
        return None

    query_polarity = classify_context_override_polarity(query)
    memory_polarity = classify_context_override_polarity(content)

    if mode == "negate":
        if query_polarity != "negative":
            return None
        if memory_polarity == "negative":
            return None
        return {
            "reason": "superseded_by_current_input",
            "override_mode": "negate",
            "approval_action": "invalidate_or_supersede",
        }

    normalized_query = normalize_match_text(query)
    normalized_content = normalize_match_text(content)
    if normalized_query and normalized_query in normalized_content:
        return None
    return {
        "reason": "superseded_by_current_input",
        "override_mode": "replace",
        "approval_action": "supersede",
    }


def extract_key_fields(memory: dict[str, Any]) -> str:
    metadata = memory.get("metadata", {}) or {}
    fields: list[str] = []
    for key in ("identity", "kind", "workspace_id", "role", "source"):
        value = metadata.get(key)
        if isinstance(value, str):
            fields.append(value)
    for item in metadata.get("source_files", []):
        if isinstance(item, str):
            fields.append(item)
    return " ".join(fields).lower()


def parse_timestamp(value: Any) -> datetime.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.UTC)
    return parsed.astimezone(datetime.UTC)


def get_half_life_days(metadata: dict[str, Any], block_name: str) -> float:
    explicit = metadata.get("half_life_days")
    if isinstance(explicit, (int, float)) and explicit > 0:
        return float(explicit)
    if metadata.get("essential") is True:
        return 3650.0
    kind = metadata.get("kind")
    scope = metadata.get("scope", "task")
    if kind in {"task_summary", "task_state", "fallback_history"} or block_name == "Task Continuation State":
        return 7.0
    if scope == "user_global":
        return 180.0
    return 30.0


def compute_lexical_score(memory: dict[str, Any], query: str) -> float:
    query_terms = parse_query_terms(query)
    if not query_terms:
        return 0.0
    content = (memory.get("content") or "").lower()
    key_fields = extract_key_fields(memory)
    normalized_query = normalize_match_text(query)
    normalized_content = normalize_match_text(memory.get("content") or "")
    exact_hits = sum(1 for term in query_terms if term in content)
    key_hits = sum(1 for term in query_terms if term in key_fields)
    phrase_hit = 1.0 if len(normalized_query) >= 4 and normalized_query in normalized_content else 0.0
    exact_term_hit_ratio = exact_hits / len(query_terms)
    key_field_hit_ratio = key_hits / len(query_terms)
    return clamp_score(
        0.55 * exact_term_hit_ratio + 0.30 * key_field_hit_ratio + 0.15 * phrase_hit
    )


def compute_semantic_score(memory: dict[str, Any]) -> float:
    metadata = memory.get("metadata", {}) or {}
    value = metadata.get("semantic_score", 0.0)
    if isinstance(value, (int, float)):
        return clamp_score(float(value))
    return 0.0


def get_semantic_api_key(config: RuntimeConfig) -> str:
    value = os.environ.get(config.semantic_api_key_env, "").strip()
    if value:
        return value
    return DEFAULT_SEMANTIC_API_KEY


def chunk_list(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def normalize_hybrid_weights(dense_weight: float, sparse_weight: float) -> tuple[float, float]:
    dense = max(0.0, dense_weight)
    sparse = max(0.0, sparse_weight)
    total = dense + sparse
    if total <= 0.0:
        return 0.7, 0.3
    return dense / total, sparse / total


def bootstrap_local_venv_site_packages() -> None:
    venv_lib_dir = ROOT_DIR / ".venv" / "lib"
    if not venv_lib_dir.exists():
        return
    for python_dir in sorted(venv_lib_dir.glob("python*/site-packages")):
        site_packages = str(python_dir.resolve())
        if site_packages not in sys.path:
            site.addsitedir(site_packages)


def get_local_venv_python() -> Path:
    return ROOT_DIR / ".venv" / "bin" / "python"


def should_use_local_bge_helper_process() -> bool:
    venv_python = get_local_venv_python()
    try:
        return not venv_python.exists() or Path(sys.executable).resolve() != venv_python.resolve()
    except OSError:
        return True


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    raw = dot / (left_norm * right_norm)
    return clamp_score((raw + 1.0) / 2.0)


def get_embedding_input_text(memory: dict[str, Any]) -> str:
    content = (memory.get("content") or "").strip()
    if content:
        return content[:4000]
    return extract_key_fields(memory)[:4000]


def request_embedding_batch(config: RuntimeConfig, inputs: list[str]) -> tuple[list[list[float]] | None, str | None]:
    if not inputs:
        return [], None
    payload = {
        "model": config.semantic_model,
        "input": inputs,
    }
    request = urllib.request.Request(
        url=f"{config.semantic_api_base}/embeddings",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {get_semantic_api_key(config)}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.semantic_timeout_seconds) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return None, f"http_error_{exc.code}"
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        return None, f"url_error_{reason}"
    except TimeoutError:
        return None, "timeout"
    except Exception as exc:  # pragma: no cover
        return None, f"embedding_exception_{type(exc).__name__}"

    data = raw.get("data")
    if not isinstance(data, list):
        return None, "invalid_embedding_response"
    vectors: list[list[float]] = []
    for item in data:
        embedding = item.get("embedding") if isinstance(item, dict) else None
        if not isinstance(embedding, list):
            return None, "invalid_embedding_item"
        vectors.append([float(value) for value in embedding])
    return vectors, None


def load_local_bge_m3_model(config: RuntimeConfig) -> tuple[Any | None, str | None]:
    global LOCAL_BGE_M3_MODEL, LOCAL_BGE_M3_LOAD_ERROR
    if LOCAL_BGE_M3_MODEL is not None:
        return LOCAL_BGE_M3_MODEL, None
    if LOCAL_BGE_M3_LOAD_ERROR is not None:
        return None, LOCAL_BGE_M3_LOAD_ERROR
    model_path = config.semantic_local_model_path
    if not model_path.exists():
        LOCAL_BGE_M3_LOAD_ERROR = f"model_path_missing:{model_path}"
        return None, LOCAL_BGE_M3_LOAD_ERROR
    cache_dir = config.semantic_local_cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    hub_cache_dir = cache_dir / "hub"
    transformers_cache_dir = cache_dir / "transformers"
    hub_cache_dir.mkdir(parents=True, exist_ok=True)
    transformers_cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(cache_dir)
    os.environ["HUGGINGFACE_HUB_CACHE"] = str(hub_cache_dir)
    os.environ["TRANSFORMERS_CACHE"] = str(transformers_cache_dir)
    try:
        bootstrap_local_venv_site_packages()
        from FlagEmbedding import BGEM3FlagModel

        LOCAL_BGE_M3_MODEL = BGEM3FlagModel(str(model_path), use_fp16=config.semantic_local_use_fp16)
        return LOCAL_BGE_M3_MODEL, None
    except Exception as exc:  # pragma: no cover
        LOCAL_BGE_M3_LOAD_ERROR = f"load_failed:{type(exc).__name__}:{exc}"
        return None, LOCAL_BGE_M3_LOAD_ERROR


def compute_local_bge_m3_hybrid_similarities_via_helper(
    config: RuntimeConfig,
    *,
    query: str,
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, float], dict[str, Any], dict[str, dict[str, float]]]:
    helper_python = get_local_venv_python()
    if not helper_python.exists():
        return {}, {
            "enabled": True,
            "status": "unavailable",
            "backend": "local_bgem3_hybrid",
            "error": f"helper_python_missing:{helper_python}",
            "model_path": str(config.semantic_local_model_path),
        }, {}
    payload = {
        "model_path": str(config.semantic_local_model_path),
        "cache_dir": str(config.semantic_local_cache_dir),
        "query": query,
        "candidates": [
            {
                "candidate_id": item["candidate_id"],
                "embedding_text": item["embedding_text"],
            }
            for item in candidates
        ],
        "batch_size": config.semantic_batch_size,
        "use_fp16": config.semantic_local_use_fp16,
        "dense_weight": config.semantic_local_dense_weight,
        "sparse_weight": config.semantic_local_sparse_weight,
        "max_query_length": config.semantic_local_max_query_length,
        "max_passage_length": config.semantic_local_max_passage_length,
    }
    timeout_seconds = max(30.0, config.semantic_timeout_seconds)
    try:
        result = subprocess.run(
            [str(helper_python), str(LOCAL_BGE_M3_HELPER_PATH)],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {}, {
            "enabled": True,
            "status": "unavailable",
            "backend": "local_bgem3_hybrid",
            "error": "helper_timeout",
            "timeout_seconds": timeout_seconds,
            "model_path": str(config.semantic_local_model_path),
        }, {}
    except Exception as exc:  # pragma: no cover
        return {}, {
            "enabled": True,
            "status": "unavailable",
            "backend": "local_bgem3_hybrid",
            "error": f"helper_invocation_failed:{type(exc).__name__}:{exc}",
            "model_path": str(config.semantic_local_model_path),
        }, {}

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        return {}, {
            "enabled": True,
            "status": "unavailable",
            "backend": "local_bgem3_hybrid",
            "error": stderr or stdout or f"helper_exit_{result.returncode}",
            "model_path": str(config.semantic_local_model_path),
            "runner": str(helper_python),
        }, {}
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}, {
            "enabled": True,
            "status": "unavailable",
            "backend": "local_bgem3_hybrid",
            "error": "helper_invalid_json",
            "model_path": str(config.semantic_local_model_path),
            "runner": str(helper_python),
        }, {}
    if raw.get("ok") is not True:
        return {}, {
            "enabled": True,
            "status": "unavailable",
            "backend": "local_bgem3_hybrid",
            "error": raw.get("error", "helper_reported_failure"),
            "model_path": str(config.semantic_local_model_path),
            "runner": str(helper_python),
        }, {}

    scores = {
        str(candidate_id): clamp_score(float(value))
        for candidate_id, value in (raw.get("scores") or {}).items()
    }
    details: dict[str, dict[str, float]] = {}
    for candidate_id, item in (raw.get("details") or {}).items():
        if not isinstance(item, dict):
            continue
        details[str(candidate_id)] = {
            "dense_score": clamp_score(float(item.get("dense_score", 0.0))),
            "sparse_score": clamp_score(float(item.get("sparse_score", 0.0))),
            "hybrid_score": clamp_score(float(item.get("hybrid_score", 0.0))),
        }
    return scores, {
        "enabled": True,
        "status": "ok",
        "backend": "local_bgem3_hybrid",
        "mode": "subprocess_venv_helper",
        "model_path": raw.get("model_path", str(config.semantic_local_model_path)),
        "cache_dir": raw.get("cache_dir", str(config.semantic_local_cache_dir)),
        "candidate_count": len(candidates),
        "embedded_count": int(raw.get("embedded_count", len(scores))),
        "dense_weight": raw.get("dense_weight"),
        "sparse_weight": raw.get("sparse_weight"),
        "runner": raw.get("runner", str(helper_python)),
    }, details


def compute_openai_embedding_similarities(
    config: RuntimeConfig,
    *,
    query: str,
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, float], dict[str, Any], dict[str, dict[str, float]]]:
    if not config.semantic_enabled:
        return {}, {"enabled": False, "status": "disabled", "backend": "openai_api"}, {}
    if not query.strip():
        return {}, {"enabled": True, "status": "skipped_empty_query", "backend": "openai_api"}, {}
    if not candidates:
        return {}, {"enabled": True, "status": "skipped_no_candidates", "backend": "openai_api"}, {}

    query_vectors, query_error = request_embedding_batch(config, [query])
    if query_error or not query_vectors:
        return {}, {
            "enabled": True,
            "status": "unavailable",
            "backend": "openai_api",
            "error": query_error or "query_embedding_failed",
            "model": config.semantic_model,
            "api_base": config.semantic_api_base,
        }, {}

    query_vector = query_vectors[0]
    candidate_texts = [item["embedding_text"] for item in candidates]
    scores: dict[str, float] = {}
    details: dict[str, dict[str, float]] = {}
    embedded_count = 0
    for batch_index, batch in enumerate(chunk_list(candidate_texts, config.semantic_batch_size)):
        vectors, error = request_embedding_batch(config, batch)
        if error or vectors is None:
            return {}, {
                "enabled": True,
                "status": "unavailable",
                "backend": "openai_api",
                "error": error or "candidate_embedding_failed",
                "model": config.semantic_model,
                "api_base": config.semantic_api_base,
                "embedded_count": embedded_count,
                "failed_batch_index": batch_index,
            }, {}
        start = batch_index * config.semantic_batch_size
        for offset, vector in enumerate(vectors):
            candidate = candidates[start + offset]
            score = cosine_similarity(query_vector, vector)
            scores[candidate["candidate_id"]] = score
            details[candidate["candidate_id"]] = {
                "dense_score": score,
                "sparse_score": 0.0,
                "hybrid_score": score,
            }
            embedded_count += 1

    return scores, {
        "enabled": True,
        "status": "ok",
        "backend": "openai_api",
        "model": config.semantic_model,
        "api_base": config.semantic_api_base,
        "candidate_count": len(candidates),
        "embedded_count": embedded_count,
    }, details


def compute_local_bge_m3_hybrid_similarities(
    config: RuntimeConfig,
    *,
    query: str,
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, float], dict[str, Any], dict[str, dict[str, float]]]:
    if not config.semantic_enabled:
        return {}, {"enabled": False, "status": "disabled", "backend": "local_bgem3_hybrid"}, {}
    if not query.strip():
        return {}, {"enabled": True, "status": "skipped_empty_query", "backend": "local_bgem3_hybrid"}, {}
    if not candidates:
        return {}, {"enabled": True, "status": "skipped_no_candidates", "backend": "local_bgem3_hybrid"}, {}

    scoped_candidates = [item for item in candidates if item.get("block_name") == "Retrieved Facts"]
    if not scoped_candidates:
        return {}, {
            "enabled": True,
            "status": "skipped_no_retrieved_facts_candidates",
            "backend": "local_bgem3_hybrid",
            "scope": "Retrieved Facts",
        }, {}

    if should_use_local_bge_helper_process():
        return compute_local_bge_m3_hybrid_similarities_via_helper(
            config,
            query=query,
            candidates=scoped_candidates,
        )

    model, load_error = load_local_bge_m3_model(config)
    if load_error or model is None:
        return {}, {
            "enabled": True,
            "status": "unavailable",
            "backend": "local_bgem3_hybrid",
            "error": load_error or "local_model_unavailable",
            "model_path": str(config.semantic_local_model_path),
        }, {}

    dense_weight, sparse_weight = normalize_hybrid_weights(
        config.semantic_local_dense_weight,
        config.semantic_local_sparse_weight,
    )
    scores: dict[str, float] = {}
    details: dict[str, dict[str, float]] = {}
    embedded_count = 0
    for batch_index, batch in enumerate(chunk_list(scoped_candidates, config.semantic_batch_size)):
        sentence_pairs = [(query, item["embedding_text"]) for item in batch]
        try:
            raw_scores = model.compute_score(
                sentence_pairs,
                batch_size=len(sentence_pairs),
                max_query_length=config.semantic_local_max_query_length,
                max_passage_length=config.semantic_local_max_passage_length,
                weights_for_different_modes=[dense_weight, sparse_weight, 0.0],
            )
        except Exception as exc:  # pragma: no cover
            return {}, {
                "enabled": True,
                "status": "unavailable",
                "backend": "local_bgem3_hybrid",
                "error": f"compute_failed:{type(exc).__name__}:{exc}",
                "model_path": str(config.semantic_local_model_path),
                "embedded_count": embedded_count,
                "failed_batch_index": batch_index,
            }, {}
        dense_values = raw_scores.get("dense", [])
        sparse_values = raw_scores.get("sparse", [])
        hybrid_values = raw_scores.get("sparse+dense", [])
        if len(hybrid_values) != len(batch):
            return {}, {
                "enabled": True,
                "status": "unavailable",
                "backend": "local_bgem3_hybrid",
                "error": "invalid_hybrid_score_response",
                "model_path": str(config.semantic_local_model_path),
                "embedded_count": embedded_count,
                "failed_batch_index": batch_index,
            }, {}
        for index, candidate in enumerate(batch):
            dense_score = clamp_score(float(dense_values[index])) if index < len(dense_values) else 0.0
            sparse_score = clamp_score(float(sparse_values[index])) if index < len(sparse_values) else 0.0
            hybrid_score = clamp_score(float(hybrid_values[index]))
            scores[candidate["candidate_id"]] = hybrid_score
            details[candidate["candidate_id"]] = {
                "dense_score": dense_score,
                "sparse_score": sparse_score,
                "hybrid_score": hybrid_score,
            }
            embedded_count += 1

    return scores, {
        "enabled": True,
        "status": "ok",
        "backend": "local_bgem3_hybrid",
        "scope": "Retrieved Facts",
        "model_path": str(config.semantic_local_model_path),
        "cache_dir": str(config.semantic_local_cache_dir),
        "candidate_count": len(candidates),
        "scoped_candidate_count": len(scoped_candidates),
        "embedded_count": embedded_count,
        "dense_weight": dense_weight,
        "sparse_weight": sparse_weight,
    }, details


def compute_semantic_similarities(
    config: RuntimeConfig,
    *,
    query: str,
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, float], dict[str, Any], dict[str, dict[str, float]]]:
    if config.semantic_backend == "local_bgem3_hybrid":
        return compute_local_bge_m3_hybrid_similarities(
            config,
            query=query,
            candidates=candidates,
        )
    return compute_openai_embedding_similarities(
        config,
        query=query,
        candidates=candidates,
    )

def compute_approval_score(memory: dict[str, Any]) -> float:
    metadata = memory.get("metadata", {}) or {}
    evidence_ids = metadata.get("evidence_ids", [])
    has_evidence = isinstance(evidence_ids, list) and bool(evidence_ids)
    source = metadata.get("source")
    if metadata.get("status") == "approved":
        base = 0.90
    elif metadata.get("approved_by"):
        base = 0.75
    elif source in {"tool_output", "user_claim", "observed_fact"}:
        base = 0.65
    elif source == "model_inference":
        base = 0.35
    else:
        base = 0.50
    if has_evidence:
        base += 0.05
    return clamp_score(base)


def compute_recency_score(memory: dict[str, Any], block_name: str) -> float:
    metadata = memory.get("metadata", {}) or {}
    timestamp = parse_timestamp(memory.get("updated_at")) or parse_timestamp(memory.get("created_at"))
    if timestamp is None:
        return 0.4
    age_days = max(0.0, (datetime.datetime.now(datetime.UTC) - timestamp).total_seconds() / 86400.0)
    half_life_days = get_half_life_days(metadata, block_name)
    return clamp_score(math.exp(-age_days / max(half_life_days, 1.0)))


def compute_role_fit_score(block_name: str, role: str | None) -> float:
    if not role:
        return 0.8
    allowed = set(ROLE_ALLOWED_BLOCKS.get(role, []))
    focus = ROLE_FOCUS_BLOCKS.get(role, allowed)
    if allowed and block_name not in allowed:
        return 0.0
    if block_name in focus:
        return 1.0
    if block_name in allowed:
        return 0.6
    return 0.3


def compute_trigger_fit_score(memory: dict[str, Any], block_name: str, trigger: str) -> float:
    metadata = memory.get("metadata", {}) or {}
    kind = metadata.get("kind")
    if trigger == "after_failure":
        if kind in {"fallback_history", "task_summary", "task_state"} or block_name in {"Known Risks", "Task Continuation State"}:
            return 1.0
        return 0.5
    if trigger == "before_task_start":
        if kind == "task_summary" or block_name in {"Workspace Facts", "Prior Decisions"}:
            return 0.8
        return 0.5
    if trigger == "on_workspace_switch":
        if block_name == "Workspace Facts":
            return 1.0
        if block_name in {"Task Continuation State", "Prior Decisions"}:
            return 0.8
        return 0.4
    return 0.6


def compute_entity_alignment_score(memory: dict[str, Any], query: str) -> float:
    entities = extract_strong_entities(query)
    if not entities:
        return 1.0
    haystack = " ".join(
        [
            (memory.get("content") or "").lower(),
            extract_key_fields(memory),
        ]
    )
    hits = sum(1 for entity in entities if entity in haystack)
    ratio = hits / len(entities)
    if ratio >= 1.0:
        return 1.0
    if ratio >= 0.5:
        return 0.5
    return 0.0


def compute_confidence_score(config: RuntimeConfig, memory: dict[str, Any], semantic_score: float) -> float:
    scoring = config.recall_scoring
    metadata = memory.get("metadata", {}) or {}
    explicit = metadata.get("confidence_score")
    if isinstance(explicit, (int, float)):
        return clamp_score(float(explicit))
    source = metadata.get("source")
    if metadata.get("status") == "approved":
        return 0.9
    if source in {"tool_output", "observed_fact", "user_claim"}:
        return 0.75
    if semantic_score >= scoring.mixed_semantic_min:
        return 0.6
    if metadata.get("kind") in {"compressed_fact", "derived_fact"}:
        return 0.8
    if source == "model_inference":
        return 0.35
    return 0.5


def role_budget_multiplier_for(config: RuntimeConfig, role: str | None) -> float:
    return float(config.recall_limits.role_budget_multipliers.get(role or "", 1.0))


def block_limit_for(config: RuntimeConfig, block_name: str) -> int:
    fallback = DEFAULT_BLOCK_LIMITS.get(block_name, 0)
    return max(0, int(config.recall_limits.block_limits.get(block_name, fallback)))


def block_priority_for(config: RuntimeConfig) -> list[str]:
    if config.recall_limits.block_priority:
        return list(config.recall_limits.block_priority)
    return list(DEFAULT_BLOCK_PRIORITY)


def lexical_floor_for_block(config: RuntimeConfig, block_name: str) -> float:
    scoring = config.recall_scoring
    if block_name == "Retrieved Facts":
        return scoring.retrieved_facts_lexical_floor
    return scoring.lexical_floor


def is_low_confidence_semantic_only(
    config: RuntimeConfig,
    lexical_score: float,
    semantic_score: float,
    *,
    block_name: str,
) -> bool:
    scoring = config.recall_scoring
    lexical_floor = lexical_floor_for_block(config, block_name)
    if block_name == "Retrieved Facts":
        return (
            lexical_score < lexical_floor
            and scoring.retrieved_facts_low_confidence_semantic_min
            <= semantic_score
            < scoring.retrieved_facts_mixed_semantic_min
        )
    return lexical_score < lexical_floor and scoring.low_confidence_semantic_min <= semantic_score < scoring.mixed_semantic_min


def estimate_item_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def render_candidate_text(memory: dict[str, Any], *, low_confidence: bool) -> str:
    content = (memory.get("content") or "").strip()
    if low_confidence and content and not content.startswith("[Low Confidence]"):
        return f"[Low Confidence] {content}"
    return content


def maybe_truncate_candidate_text(text: str) -> str:
    if estimate_item_tokens(text) <= MAX_ITEM_TOKENS:
        return text
    max_chars = MAX_ITEM_TOKENS * 4
    truncated = text[:max_chars].rstrip()
    if truncated.endswith("..."):
        return truncated
    return f"{truncated}..."


def passes_hard_filters(
    memory: dict[str, Any],
    *,
    block_name: str,
    scope: str,
    workspace_id: str,
    trigger: str,
) -> tuple[bool, str | None]:
    metadata = memory.get("metadata", {}) or {}
    memory_state = memory.get("state")
    metadata_state = metadata.get("state")
    if memory_state in {"archived", "deleted"}:
        return False, "memory_state_filtered"
    if metadata_state in {"archived", "deleted", "outdated"}:
        return False, "metadata_state_filtered"
    if metadata_state == "superseded" and not metadata.get("essential"):
        return False, "superseded"
    item_workspace = metadata.get("workspace_id")
    if scope in {"workspace", "task"} and item_workspace not in {None, workspace_id} and not metadata.get("cross_workspace"):
        return False, "workspace_mismatch"
    if trigger == "before_task_start" and block_name == "Task Continuation State":
        if metadata.get("kind") not in {"task_summary", "task_state", "fallback_history"} and scope == "task":
            return False, "before_task_start_task_filter"
    return True, None


def rank_memory_candidate(
    config: RuntimeConfig,
    memory: dict[str, Any],
    *,
    block_name: str,
    role: str | None,
    query: str,
    trigger: str,
    semantic_score_override: float | None = None,
) -> dict[str, Any]:
    lexical_score = compute_lexical_score(memory, query)
    semantic_score = (
        clamp_score(float(semantic_score_override))
        if isinstance(semantic_score_override, (int, float))
        else compute_semantic_score(memory)
    )
    approval_score = compute_approval_score(memory)
    recency_score = compute_recency_score(memory, block_name)
    role_fit_score = compute_role_fit_score(block_name, role)
    trigger_fit_score = compute_trigger_fit_score(memory, block_name, trigger)
    entity_alignment_score = compute_entity_alignment_score(memory, query)
    confidence_score = compute_confidence_score(config, memory, semantic_score)
    scoring = config.recall_scoring

    final_score = clamp_score(
        scoring.lexical_weight * lexical_score
        + scoring.semantic_weight * semantic_score
        + scoring.approval_weight * approval_score
        + scoring.recency_weight * recency_score
        + scoring.role_fit_weight * role_fit_score
        + scoring.trigger_fit_weight * trigger_fit_score
        + scoring.entity_alignment_weight * entity_alignment_score
        + scoring.confidence_weight * confidence_score
    )

    return {
        "lexical_score": lexical_score,
        "semantic_score": semantic_score,
        "approval_score": approval_score,
        "recency_score": recency_score,
        "role_fit_score": role_fit_score,
        "trigger_fit_score": trigger_fit_score,
        "entity_alignment_score": entity_alignment_score,
        "confidence_score": confidence_score,
        "final_score": final_score,
        "low_confidence": is_low_confidence_semantic_only(
            config,
            lexical_score,
            semantic_score,
            block_name=block_name,
        ),
    }


def passes_retrieval_signal_gate(
    config: RuntimeConfig,
    scores: dict[str, Any],
    *,
    block_name: str,
    has_strong_entities: bool,
) -> tuple[bool, str]:
    scoring = config.recall_scoring
    if scores["lexical_score"] >= lexical_floor_for_block(config, block_name):
        return True, "lexical"
    semantic_score = scores["semantic_score"]
    if block_name == "Retrieved Facts":
        if semantic_score >= scoring.retrieved_facts_mixed_semantic_min:
            return True, "semantic_mixed"
        if (
            semantic_score >= scoring.retrieved_facts_low_confidence_semantic_min
            and (
                scores["lexical_score"] >= scoring.low_confidence_lexical_hint
                or has_strong_entities
            )
        ):
            return True, "semantic_low_confidence"
    elif semantic_score >= scoring.mixed_semantic_min:
        return True, "semantic_mixed"
    if block_name != "Retrieved Facts" and scores["final_score"] >= scoring.final_score_min:
        return True, "final_score_backstop"
    return False, "below_retrieval_signal_threshold"


def trim_items(items: list[str], limit: int) -> list[str]:
    return items[:limit]


def build_memory_context_provider_deps() -> MemoryContextProviderDeps:
    return MemoryContextProviderDeps(
        list_memories=list_memories,
        explain_memory_classification=explain_memory_classification,
        passes_hard_filters=passes_hard_filters,
        get_embedding_input_text=get_embedding_input_text,
        compute_semantic_similarities=compute_semantic_similarities,
        rank_memory_candidate=rank_memory_candidate,
        passes_retrieval_signal_gate=passes_retrieval_signal_gate,
        render_candidate_text=render_candidate_text,
        maybe_truncate_candidate_text=maybe_truncate_candidate_text,
        block_limit_for=block_limit_for,
        role_budget_multiplier_for=role_budget_multiplier_for,
        block_priority_for=block_priority_for,
        evaluate_context_override=evaluate_context_override,
        estimate_item_tokens=estimate_item_tokens,
        parse_query_terms=parse_query_terms,
        extract_strong_entities=extract_strong_entities,
        resolve_trigger=resolve_trigger,
    )


def build_context_result(
    config: RuntimeConfig,
    *,
    user_id: str,
    workspace_id: str,
    role: str | None,
    query: str,
    trigger: str,
) -> dict[str, Any]:
    scoring = config.recall_scoring
    memories = list_memories(config, user_id)
    blocks: dict[str, list[dict[str, Any]]] = {
        "Relevant Preferences": [],
        "Workspace Facts": [],
        "Prior Decisions": [],
        "Task Continuation State": [],
        "Known Risks": [],
        "Retrieved Facts": [],
    }
    debug_entries: list[dict[str, Any]] = []
    query_terms = parse_query_terms(query)
    strong_entities = extract_strong_entities(query)
    semantic_candidates: list[dict[str, Any]] = []

    for memory in memories:
        block_name, scope, classification_reason = explain_memory_classification(memory, workspace_id)
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
        keep, reason = passes_hard_filters(
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
                "embedding_text": get_embedding_input_text(memory),
                "memory": memory,
                "debug_entry": debug_entry,
                "block_name": block_name,
                "scope": scope,
            }
        )

    semantic_scores, semantic_debug, semantic_details = compute_semantic_similarities(
        config,
        query=query,
        candidates=semantic_candidates,
    )

    for semantic_candidate in semantic_candidates:
        memory = semantic_candidate["memory"]
        debug_entry = semantic_candidate["debug_entry"]
        block_name = semantic_candidate["block_name"]
        scope = semantic_candidate["scope"]
        metadata = memory.get("metadata", {}) or {}

        scores = rank_memory_candidate(
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
        admitted, admit_reason = passes_retrieval_signal_gate(
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

        text = render_candidate_text(memory, low_confidence=scores["low_confidence"])
        text = maybe_truncate_candidate_text(text)
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
        block_limit = block_limit_for(config, name)
        for candidate in ordered_candidates[block_limit:]:
            candidate["debug_entry"]["status"] = "filtered"
            candidate["debug_entry"]["reason"] = "block_limit_trim"
        deduped_blocks[name] = ordered_candidates[:block_limit]

    stale_or_superseded: list[dict[str, Any]] = []
    for block_name in CONTEXT_OVERRIDE_BLOCKS:
        filtered_candidates: list[dict[str, Any]] = []
        for candidate in deduped_blocks[block_name]:
            override = evaluate_context_override(
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

    role_multiplier = role_budget_multiplier_for(config, role)
    total_budget = max(1, int(config.max_long_term_context_tokens * role_multiplier))
    used_tokens = 0
    selected_by_block: dict[str, list[str]] = {name: [] for name in blocks}
    for block_name in block_priority_for(config):
        for candidate in deduped_blocks[block_name]:
            candidate_tokens = estimate_item_tokens(candidate["text"])
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
        trimmed = trim_items(values, block_limit_for(config, name))
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
                "block_limits": {
                    name: block_limit_for(config, name) for name in blocks
                },
                "block_priority": block_priority_for(config),
            },
            "per_memory": debug_entries,
        },
    }


def build_context_blocks(
    config: RuntimeConfig,
    *,
    user_id: str,
    workspace_id: str,
    role: str | None,
    query: str,
    trigger: str,
) -> dict[str, list[str]]:
    bundle = provider_build_context_bundle(
        config,
        build_memory_context_provider_deps(),
        user_id=user_id,
        workspace_id=workspace_id,
        role=role,
        query=query,
        trigger=trigger,
        context_override_blocks=CONTEXT_OVERRIDE_BLOCKS,
        role_allowed_blocks=ROLE_ALLOWED_BLOCKS,
        debug=False,
    )
    return bundle["context_blocks"]


def select_role_blocks(blocks: dict[str, list[str]], role: str | None) -> dict[str, list[str]]:
    return provider_select_role_blocks(blocks, role=role, role_allowed_blocks=ROLE_ALLOWED_BLOCKS)


def render_context_text(blocks: dict[str, list[str]], *, workspace_id: str, query: str) -> str:
    return provider_render_context_text(blocks, workspace_id=workspace_id, query=query)


def estimate_tokens(blocks: dict[str, list[str]], query: str) -> int:
    return provider_estimate_tokens(blocks, query)


def build_state_key(user_id: str, workspace_id: str) -> str:
    return f"{user_id}:{workspace_id}"


def resolve_workspace_id(config: RuntimeConfig, cwd: str | None, workspace_id: str | None) -> str:
    if workspace_id:
        return workspace_id
    if cwd:
        return Path(cwd).resolve().name
    return config.default_workspace_id


def resolve_trigger(
    config: RuntimeConfig,
    *,
    user_id: str,
    workspace_id: str,
    trigger: str,
) -> str:
    if trigger != "auto":
        return trigger
    state = load_state(config)
    last_workspace = state.get("__meta", {}).get("last_workspace_by_user", {}).get(user_id)
    if last_workspace and last_workspace != workspace_id:
        return "on_workspace_switch"
    return "before_task_start"


def derive_lifecycle_stage(trigger: str | None) -> str:
    if trigger in LIFECYCLE_STAGE_BY_TRIGGER:
        return LIFECYCLE_STAGE_BY_TRIGGER[str(trigger)]
    return "active"


def normalize_run_state_bucket(bucket: dict[str, Any] | None) -> dict[str, Any]:
    if not bucket:
        return {}
    normalized = dict(bucket)
    normalized["lifecycle_stage"] = normalized.get("lifecycle_stage") or derive_lifecycle_stage(
        normalized.get("last_trigger")
    )
    return normalized


def update_run_state(
    config: RuntimeConfig,
    *,
    user_id: str,
    workspace_id: str,
    role: str | None,
    trigger: str,
    context_blocks: dict[str, list[str]],
    query: str,
    fallback: str | None = None,
) -> dict[str, Any]:
    state = load_state(config)
    workspaces = state.setdefault("workspaces", {})
    key = build_state_key(user_id, workspace_id)
    bucket = workspaces.get(
        key,
        {
            "user_id": user_id,
            "workspace_id": workspace_id,
            "lifecycle_stage": derive_lifecycle_stage(trigger),
            "active_roles": [],
            "fallback_history": [],
            "memory_budget": {},
            "task_summary": "",
        },
    )
    if role and role not in bucket["active_roles"]:
        bucket["active_roles"].append(role)
    bucket["run_revision"] = int(bucket.get("run_revision", 0)) + 1
    bucket["context_snapshot_id"] = uuid.uuid4().hex
    bucket["last_trigger"] = trigger
    bucket["lifecycle_stage"] = derive_lifecycle_stage(trigger)
    bucket["last_query"] = query[:220]
    bucket["last_context_blocks"] = context_blocks
    bucket["updated_at"] = int(time.time())
    if fallback:
        bucket["fallback_history"] = [fallback, *bucket.get("fallback_history", [])][:5]
    workspaces[key] = bucket
    meta = state.setdefault("__meta", {})
    meta.setdefault("last_workspace_by_user", {})[user_id] = workspace_id
    meta["updated_at"] = int(time.time())
    save_state(config, state)
    return normalize_run_state_bucket(bucket)


def build_context_package(
    config: RuntimeConfig,
    *,
    user_id: str,
    workspace_id: str,
    role: str | None,
    query: str,
    trigger: str,
) -> dict[str, Any]:
    bundle = provider_build_context_bundle(
        config,
        build_memory_context_provider_deps(),
        user_id=user_id,
        workspace_id=workspace_id,
        role=role,
        query=query,
        trigger=trigger,
        context_override_blocks=CONTEXT_OVERRIDE_BLOCKS,
        role_allowed_blocks=ROLE_ALLOWED_BLOCKS,
        debug=False,
    )
    run_state = update_run_state(
        config,
        user_id=user_id,
        workspace_id=workspace_id,
        role=role,
        trigger=bundle["trigger"],
        context_blocks=bundle["context_blocks"],
        query=query,
    )
    result = dict(bundle)
    result["context_snapshot_id"] = run_state.get("context_snapshot_id")
    result["run_revision"] = run_state.get("run_revision")
    result["run_state"] = run_state
    return result


def build_context_debug_package(
    config: RuntimeConfig,
    *,
    user_id: str,
    workspace_id: str,
    role: str | None,
    query: str,
    trigger: str,
) -> dict[str, Any]:
    bundle = provider_build_context_bundle(
        config,
        build_memory_context_provider_deps(),
        user_id=user_id,
        workspace_id=workspace_id,
        role=role,
        query=query,
        trigger=trigger,
        context_override_blocks=CONTEXT_OVERRIDE_BLOCKS,
        role_allowed_blocks=ROLE_ALLOWED_BLOCKS,
        debug=True,
    )
    run_state = update_run_state(
        config,
        user_id=user_id,
        workspace_id=workspace_id,
        role=role,
        trigger=bundle["trigger"],
        context_blocks=bundle["context_blocks"],
        query=query,
    )
    result = dict(bundle)
    result["context_snapshot_id"] = run_state.get("context_snapshot_id")
    result["run_revision"] = run_state.get("run_revision")
    result["run_state"] = run_state
    return result


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


def render_memory_content(item: dict[str, Any]) -> str:
    kind = item.get("kind")
    if kind in {"facts", "preferences"}:
        return f"{item.get('key')}: {item.get('value')}"
    if kind == "decisions":
        return f"[{item.get('proposal_type')}] {item.get('topic')}: {item.get('decision')}"
    if kind == "risks":
        return str(item.get("risk"))
    return item.get("legacy_text", "")


def index_existing_workspace_memories_by_identity(
    memories: list[dict[str, Any]],
    *,
    workspace_id: str,
    active_only: bool = True,
    require_high_confidence: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    existing_by_identity: dict[str, list[dict[str, Any]]] = {}
    for memory in memories:
        metadata = memory.get("metadata", {}) or {}
        identity = metadata.get("identity")
        if not identity:
            continue
        if metadata.get("workspace_id", workspace_id) != workspace_id:
            continue
        if active_only and memory.get("state") != "active":
            continue
        if require_high_confidence and not is_high_confidence_existing_memory(memory):
            continue
        existing_by_identity.setdefault(identity, []).append(memory)
    return existing_by_identity


def is_high_confidence_existing_memory(memory: dict[str, Any]) -> bool:
    metadata = memory.get("metadata", {}) or {}
    if metadata.get("status") != "approved":
        return False
    confidence = metadata.get("confidence_score")
    if isinstance(confidence, (int, float)):
        return float(confidence) >= 0.85
    return True


def resolve_writeback_confidence_score(item: dict[str, Any], approved: dict[str, Any]) -> float:
    explicit = item.get("confidence_score")
    if isinstance(explicit, (int, float)):
        return clamp_score(float(explicit))

    source = item.get("source") or approved.get("source")
    evidence_ids = item.get("evidence_ids")
    if not isinstance(evidence_ids, list):
        evidence_ids = approved.get("evidence_ids", [])
    has_evidence = bool(evidence_ids)

    if item.get("proposal_kind") == "compression_proposal" or isinstance(item.get("compression_manifest"), dict):
        return 0.8
    if source == "user_claim":
        return 1.0
    if source == "approved_decision":
        return 0.9
    if source in {"observed_fact", "tool_output"}:
        return 0.9 if has_evidence else 0.82
    if source == "model_inference":
        return 0.35
    if has_evidence:
        return 0.85
    return 0.7


def build_context_override_writeback_plan(
    config: RuntimeConfig,
    *,
    user_id: str,
    workspace_id: str,
    context_package: dict[str, Any] | None,
    merged_state: dict[str, list[dict[str, Any]]],
    approval_report: dict[str, Any],
) -> dict[str, Any]:
    if not context_package:
        return {
            "ok": True,
            "actions": [],
            "warnings": [],
            "context_snapshot_id": None,
            "run_revision": None,
        }

    package_workspace_id = context_package.get("workspace_id")
    if isinstance(package_workspace_id, str) and package_workspace_id != workspace_id:
        return {
            "ok": False,
            "actions": [],
            "warnings": [
                {
                    "reason": "context_package_workspace_mismatch",
                    "package_workspace_id": package_workspace_id,
                    "requested_workspace_id": workspace_id,
                }
            ],
            "context_snapshot_id": context_package.get("context_snapshot_id"),
            "run_revision": context_package.get("run_revision"),
        }

    stale_items = context_package.get("stale_or_superseded", [])
    if not isinstance(stale_items, list):
        return {
            "ok": False,
            "actions": [],
            "warnings": [{"reason": "context_package_stale_or_superseded_invalid"}],
            "context_snapshot_id": context_package.get("context_snapshot_id"),
            "run_revision": context_package.get("run_revision"),
        }

    existing = list_memories(config, user_id)
    existing_by_identity = index_existing_workspace_memories_by_identity(
        existing,
        workspace_id=workspace_id,
        active_only=True,
        require_high_confidence=False,
    )
    approved_identities = {item["identity"] for item in approval_report.get("approved", [])}
    merged_index = {
        item["identity"]: item
        for key in ("facts", "preferences", "decisions", "risks")
        for item in merged_state.get(key, [])
    }

    actions_by_identity: dict[str, dict[str, Any]] = {}
    warnings: list[dict[str, Any]] = []
    for item in stale_items:
        if not isinstance(item, dict):
            continue
        identity = item.get("identity")
        override_mode = item.get("override_mode")
        approval_action = item.get("approval_action")
        if not isinstance(identity, str) or not identity:
            continue
        if override_mode not in {"negate", "replace"}:
            continue

        existing_matches = existing_by_identity.get(identity, [])
        if not existing_matches:
            warnings.append(
                {
                    "identity": identity,
                    "reason": "context_override_no_active_memory_found",
                }
            )
            continue

        has_replacement = identity in approved_identities and identity in merged_index
        if override_mode == "replace" and not has_replacement:
            warnings.append(
                {
                    "identity": identity,
                    "reason": "context_override_replace_requires_replacement_candidate",
                }
            )
            continue

        strategy = "archive_then_create" if has_replacement else "archive_only"
        actions_by_identity[identity] = {
            "identity": identity,
            "override_mode": override_mode,
            "approval_action": approval_action,
            "strategy": strategy,
            "existing_memory_ids": [memory["id"] for memory in existing_matches],
            "existing_contents": [memory.get("content", "") for memory in existing_matches],
            "preview": item.get("preview"),
        }

    return {
        "ok": True,
        "actions": list(actions_by_identity.values()),
        "warnings": warnings,
        "context_snapshot_id": context_package.get("context_snapshot_id"),
        "run_revision": context_package.get("run_revision"),
        "query": context_package.get("query"),
    }


def detect_pre_writeback_conflicts(
    config: RuntimeConfig,
    *,
    user_id: str,
    workspace_id: str,
    merged_state: dict[str, list[dict[str, Any]]],
    approval_report: dict[str, Any],
    context_override_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = list_memories(config, user_id)
    existing_by_identity = index_existing_workspace_memories_by_identity(
        existing,
        workspace_id=workspace_id,
        active_only=True,
        require_high_confidence=True,
    )
    override_identities = {
        item["identity"]
        for item in (context_override_plan or {}).get("actions", [])
        if item.get("strategy") == "archive_then_create"
    }

    merged_index = {
        item["identity"]: item
        for key in ("facts", "preferences", "decisions", "risks")
        for item in merged_state.get(key, [])
    }

    conflicts: list[dict[str, Any]] = []
    for approved in approval_report.get("approved", []):
        proposal_type = approved.get("proposal_type")
        if proposal_type in {"update", "invalidate"}:
            continue
        identity = approved["identity"]
        if identity in override_identities:
            continue
        item = merged_index.get(identity)
        if not item or item.get("kind") not in {"facts", "preferences", "decisions"}:
            continue
        proposed_content = render_memory_content(item).strip()
        if not proposed_content:
            continue
        for previous in existing_by_identity.get(identity, []):
            previous_content = (previous.get("content") or "").strip()
            if not previous_content or previous_content == proposed_content:
                continue
            conflicts.append(
                {
                    "identity": identity,
                    "kind": item.get("kind"),
                    "proposal_type": proposal_type,
                    "existing_memory_id": previous.get("id"),
                    "existing_content": previous_content,
                    "proposed_content": proposed_content,
                    "reason": "existing_active_memory_with_same_identity_differs",
                    "recommended_action": "use_update_or_invalidate",
                }
            )
            break

    return {
        "ok": not conflicts,
        "conflicts": conflicts,
    }


def writeback_approved_items(
    config: RuntimeConfig,
    *,
    user_id: str,
    workspace_id: str,
    merged_state: dict[str, list[dict[str, Any]]],
    approval_report: dict[str, Any],
    context_override_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = list_memories(config, user_id)
    existing_by_identity = index_existing_workspace_memories_by_identity(
        existing,
        workspace_id=workspace_id,
        active_only=False,
        require_high_confidence=False,
    )
    override_actions_by_identity = {
        item["identity"]: item for item in (context_override_plan or {}).get("actions", [])
    }

    merged_index = {
        item["identity"]: item
        for key in ("facts", "preferences", "decisions", "risks")
        for item in merged_state.get(key, [])
    }

    writeback: list[dict[str, Any]] = []
    archived_previous_ids: set[str] = set()
    for approved in approval_report.get("approved", []):
        identity = approved["identity"]
        item = merged_index.get(identity)
        if not item:
            continue
        override_action = override_actions_by_identity.get(identity)
        proposal_type = approved.get("proposal_type")
        effective_proposal_type = proposal_type
        if proposal_type in {"update", "invalidate"}:
            effective_proposal_type = proposal_type
        elif override_action and override_action.get("strategy") == "archive_then_create":
            effective_proposal_type = "update"

        archived_ids_for_identity: list[str] = []
        for previous in existing_by_identity.get(identity, []):
            previous_id = previous["id"]
            if previous_id in archived_previous_ids:
                continue
            if effective_proposal_type in {"update", "invalidate"} and previous.get("state") == "active":
                update_memory(config, previous_id, state="archived")
                archived_previous_ids.add(previous_id)
                archived_ids_for_identity.append(previous_id)

        if effective_proposal_type == "invalidate":
            writeback.append(
                {
                    "identity": identity,
                    "action": "archived_previous_only",
                    "archived_memory_ids": archived_ids_for_identity,
                    "context_override_applied": bool(override_action),
                }
            )
            continue

        content = render_memory_content(item)
        metadata = {
            "protocol": "v1.1",
            "scope": item.get("scope", approved.get("scope")),
            "kind": item.get("kind"),
            "identity": identity,
            "proposal_type": effective_proposal_type,
            "source": item.get("source"),
            "workspace_id": workspace_id,
            "status": "approved",
            "approved_by": "main_agent_runtime",
            "evidence_ids": item.get("evidence_ids", []),
            "source_files": approved.get("source_files", []),
            "approved_at": int(time.time()),
            "confidence_score": resolve_writeback_confidence_score(item, approved),
        }
        created = add_memory(config, user_id, content, metadata)
        writeback.append(
            {
                "identity": identity,
                "action": "archived_previous_and_created" if archived_ids_for_identity else "created",
                "archived_memory_ids": archived_ids_for_identity,
                "context_override_applied": bool(override_action),
                "memory": created,
            }
        )

    for identity, override_action in override_actions_by_identity.items():
        if override_action.get("strategy") != "archive_only":
            continue
        archived_ids_for_identity: list[str] = []
        for previous in existing_by_identity.get(identity, []):
            previous_id = previous["id"]
            if previous_id in archived_previous_ids:
                continue
            if previous.get("state") != "active":
                continue
            update_memory(config, previous_id, state="archived")
            archived_previous_ids.add(previous_id)
            archived_ids_for_identity.append(previous_id)
        if archived_ids_for_identity:
            writeback.append(
                {
                    "identity": identity,
                    "action": "archived_previous_only",
                    "archived_memory_ids": archived_ids_for_identity,
                    "context_override_applied": True,
                    "override_mode": override_action.get("override_mode"),
                }
            )

    should_store_audit_record = bool(approval_report.get("audit_records")) or bool(
        (context_override_plan or {}).get("actions")
    )
    if config.store_audit_records and should_store_audit_record:
        audit_content = json.dumps(
            {
                "approved": approval_report.get("approved", []),
                "deferred": approval_report.get("deferred", []),
                "rejected": approval_report.get("rejected", []),
                "context_override_actions": (context_override_plan or {}).get("actions", []),
            },
            ensure_ascii=False,
        )
        audit_metadata = {
            "protocol": "v1.1",
            "kind": "audit_record",
            "scope": "task",
            "workspace_id": workspace_id,
            "status": "approved",
            "approved_by": "main_agent_runtime",
            "approved_at": int(time.time()),
            "confidence_score": 1.0,
        }
        created = add_memory(config, user_id, audit_content, audit_metadata)
        writeback.append({"identity": "audit_record", "action": "created", "memory": created})

    return {"writeback": writeback}


def approve_payloads(
    config: RuntimeConfig,
    *,
    payloads: list[dict[str, Any]],
    user_id: str,
    workspace_id: str,
    writeback: bool,
    context_package: dict[str, Any] | None = None,
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
        approval_report = (
            VALIDATOR.build_approval_report(merged_state)
            if accepted_payloads
            else {"approved": [], "deferred": [], "rejected": [], "audit_records": [], "ok": False}
        )
        fallback_report = (
            VALIDATOR.build_fallback_report(accepted_payloads, accepted_files)
            if accepted_payloads
            else {"ok": True, "routes": []}
        )
        role_context_report = (
            VALIDATOR.build_role_context_report(accepted_payloads, accepted_files)
            if accepted_payloads
            else {"ok": True, "per_file": []}
        )
        snapshot_consistency_report = (
            VALIDATOR.build_snapshot_consistency_report(accepted_payloads, accepted_files)
            if accepted_payloads
            else {"ok": True, "role_payloads": 0, "snapshot_group_count": 0, "per_file": [], "violations": []}
        )
        budget_report = (
            VALIDATOR.build_budget_report(
                accepted_payloads,
                accepted_files,
                max_stage_context_tokens=config.max_stage_context_tokens,
                max_long_term_tokens=config.max_long_term_context_tokens,
            )
            if accepted_payloads
            else {"ok": True, "violations": []}
        )
        assertions = (
            VALIDATOR.build_contract_assertions(
                accepted_payloads,
                role_context_report=role_context_report,
                snapshot_consistency_report=snapshot_consistency_report,
                budget_report=budget_report,
                approval_report=approval_report,
                fallback_report=fallback_report,
            )
            if accepted_payloads
            else {}
        )

        context_override_plan = build_context_override_writeback_plan(
            config,
            user_id=user_id,
            workspace_id=workspace_id,
            context_package=context_package,
            merged_state=merged_state,
            approval_report=approval_report,
        )
        context_override_ready = context_override_plan.get("ok", False) and bool(context_override_plan.get("actions"))

        result_ok = not validation_errors and all(assertions.values()) if assertions else False
        if not accepted_payloads and context_override_ready:
            result_ok = True
        result: dict[str, Any] = {
            "ok": result_ok,
            "validation_errors": validation_errors,
            "merged_state": merged_state,
            "approval_report": approval_report,
            "fallback_report": fallback_report,
            "role_context_report": role_context_report,
            "snapshot_consistency_report": snapshot_consistency_report,
            "budget_report": budget_report,
            "contract_assertions": assertions,
        }
        if context_package is not None:
            result["context_override_report"] = context_override_plan

        pre_writeback_conflicts = (
            detect_pre_writeback_conflicts(
                config,
                user_id=user_id,
                workspace_id=workspace_id,
                merged_state=merged_state,
                approval_report=approval_report,
                context_override_plan=context_override_plan,
            )
            if writeback and result_ok and approval_report.get("approved") and config.writeback_enabled
            else {"ok": True, "conflicts": []}
        )
        if pre_writeback_conflicts["conflicts"]:
            result["pre_writeback_conflicts"] = pre_writeback_conflicts

        can_writeback = bool(approval_report.get("approved")) or context_override_ready
        if writeback and result_ok and can_writeback and config.writeback_enabled:
            if pre_writeback_conflicts["conflicts"]:
                result["writeback_blocked_reason"] = "writeback_conflict_requires_review"
            else:
                result["writeback_report"] = writeback_approved_items(
                    config,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    merged_state=merged_state,
                    approval_report=approval_report,
                    context_override_plan=context_override_plan,
                )
        elif writeback and can_writeback and config.writeback_enabled and not result_ok:
            result["writeback_blocked_reason"] = "writeback_requires_ok_true"
        return result
    finally:
        temp_dir.cleanup()


def load_payload_inputs(paths: list[str], *, allow_empty: bool = False) -> list[dict[str, Any]]:
    if paths:
        return [json.loads(Path(path).read_text(encoding="utf-8")) for path in paths]

    raw = sys.stdin.read().strip()
    if not raw:
        if allow_empty:
            return []
        raise RuntimeError("no payload input provided")
    payload = json.loads(raw)
    if isinstance(payload, dict) and "payloads" in payload:
        payload = payload["payloads"]
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return payload
    raise RuntimeError("payload input must be a JSON object or array")


def compact_memories_dry_run(
    config: RuntimeConfig,
    *,
    user_id: str,
    workspace_id: str,
    target_scope: str,
    memory_ids: list[str],
    limit: int,
    key: str | None = None,
) -> dict[str, Any]:
    memories = list_memories(config, user_id)
    result = build_compaction_dry_run(
        memories,
        workspace_id=workspace_id,
        target_scope=target_scope,
        requested_memory_ids=memory_ids,
        limit=limit,
        key=key,
    )
    proposal = result.get("proposal")
    if not isinstance(proposal, dict):
        result["validation_errors"] = []
        result["approval_report"] = {"approved": [], "deferred": [], "rejected": [], "audit_records": [], "ok": False}
        result["contract_assertions"] = {}
        result["budget_report"] = {"ok": True, "violations": []}
        result["writeback_enabled"] = False
        return result

    source_path = ROOT_DIR / "runtime" / "compact-dry-run.generated.json"
    validation_errors = VALIDATOR.validate_payload(proposal, source_path)
    if validation_errors:
        result["ok"] = False
        result["validation_errors"] = validation_errors
        result["approval_report"] = {"approved": [], "deferred": [], "rejected": [], "audit_records": [], "ok": False}
        result["contract_assertions"] = {}
        result["budget_report"] = {"ok": True, "violations": []}
        result["writeback_enabled"] = False
        return result

    merged_state = VALIDATOR.merge_payloads([proposal], [source_path])
    approval_report = VALIDATOR.build_approval_report(merged_state)
    fallback_report = VALIDATOR.build_fallback_report([proposal], [source_path])
    role_context_report = VALIDATOR.build_role_context_report([proposal], [source_path])
    snapshot_consistency_report = VALIDATOR.build_snapshot_consistency_report([proposal], [source_path])
    budget_report = VALIDATOR.build_budget_report(
        [proposal],
        [source_path],
        max_stage_context_tokens=config.max_stage_context_tokens,
        max_long_term_tokens=config.max_long_term_context_tokens,
    )
    assertions = VALIDATOR.build_contract_assertions(
        [proposal],
        role_context_report=role_context_report,
        snapshot_consistency_report=snapshot_consistency_report,
        budget_report=budget_report,
        approval_report=approval_report,
        fallback_report=fallback_report,
    )
    result["validation_errors"] = validation_errors
    result["merged_state"] = merged_state
    result["approval_report"] = approval_report
    result["fallback_report"] = fallback_report
    result["role_context_report"] = role_context_report
    result["snapshot_consistency_report"] = snapshot_consistency_report
    result["budget_report"] = budget_report
    result["contract_assertions"] = assertions
    result["writeback_enabled"] = False
    result["ok"] = bool(approval_report.get("approved")) and approval_report.get("ok", False) and all(assertions.values())
    return result


def emit_json(payload: dict[str, Any], output: str | None) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if output:
        Path(output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Main-agent runtime helper for v1.1 memory recall/injection")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="Path to runtime config TOML")

    subparsers = parser.add_subparsers(dest="command", required=True)

    recall = subparsers.add_parser("recall", help="Build role-aware long-term context blocks")
    recall.add_argument("--user", dest="user_id")
    recall.add_argument("--workspace-id")
    recall.add_argument("--cwd")
    recall.add_argument("--role")
    recall.add_argument(
        "--trigger",
        default="auto",
        choices=["auto", "before_task_start", "after_failure", "on_workspace_switch", "before_subagent_spawn"],
    )
    recall.add_argument("--query", default="")
    recall.add_argument("--output")

    recall_debug = subparsers.add_parser("recall-debug", help="Show recall candidates, filters, and scoring details")
    recall_debug.add_argument("--user", dest="user_id")
    recall_debug.add_argument("--workspace-id")
    recall_debug.add_argument("--cwd")
    recall_debug.add_argument("--role")
    recall_debug.add_argument(
        "--trigger",
        default="auto",
        choices=["auto", "before_task_start", "after_failure", "on_workspace_switch", "before_subagent_spawn"],
    )
    recall_debug.add_argument("--query", default="")
    recall_debug.add_argument("--output")

    approve = subparsers.add_parser("approve", help="Validate, approve, and optionally write back subagent payloads")
    approve.add_argument("payload_files", nargs="*")
    approve.add_argument("--user", dest="user_id")
    approve.add_argument("--workspace-id")
    approve.add_argument("--cwd")
    approve.add_argument("--context-package")
    approve.add_argument("--writeback", action="store_true")
    approve.add_argument("--output")

    runstate = subparsers.add_parser("runstate", help="Show current runtime state bucket")
    runstate.add_argument("--user", dest="user_id")
    runstate.add_argument("--workspace-id")
    runstate.add_argument("--cwd")
    runstate.add_argument("--output")

    finalize = subparsers.add_parser("finalize", help="Mark stale task continuation state as outdated")
    finalize.add_argument("--user", dest="user_id")
    finalize.add_argument("--workspace-id")
    finalize.add_argument("--cwd")
    finalize.add_argument("--query", default="")
    finalize.add_argument("--output")

    compact = subparsers.add_parser("compact", help="Generate a compression proposal dry-run without writeback")
    compact.add_argument("--user", dest="user_id")
    compact.add_argument("--workspace-id")
    compact.add_argument("--cwd")
    compact.add_argument("--scope", choices=["task", "workspace", "user_global"], default="workspace")
    compact.add_argument("--memory-id", action="append", dest="memory_ids", default=[])
    compact.add_argument("--limit", type=int, default=4)
    compact.add_argument("--key")
    compact.add_argument("--dry-run", action="store_true")
    compact.add_argument("--output")

    backfill_confidence = subparsers.add_parser(
        "backfill-confidence",
        help="Dry-run or apply historical confidence_score backfill for approved long-term memories",
    )
    backfill_confidence.add_argument("--user", dest="user_id")
    backfill_confidence.add_argument("--workspace-id")
    backfill_confidence.add_argument("--ids-file")
    backfill_confidence.add_argument("--limit", type=int)
    backfill_confidence.add_argument("--mode", choices=["dry-run", "apply"], default="dry-run")
    backfill_confidence.add_argument("--output")

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(Path(args.config).resolve())
    middleware_deps = RuntimeMiddlewareDeps(
        resolve_workspace_id=resolve_workspace_id,
        build_context_package=build_context_package,
        build_context_debug_package=build_context_debug_package,
        load_payload_inputs=load_payload_inputs,
        approve_payloads=approve_payloads,
        finalize_task_state=finalize_task_state,
    )

    if args.command == "recall":
        user_id = args.user_id or config.default_user_id
        result = run_recall_middleware(
            config,
            middleware_deps,
            user_id=user_id,
            workspace_id=args.workspace_id,
            cwd=args.cwd,
            role=args.role,
            query=args.query.strip(),
            trigger=args.trigger,
        )
        emit_json(result, args.output)
        return 0

    if args.command == "recall-debug":
        user_id = args.user_id or config.default_user_id
        result = run_recall_middleware(
            config,
            middleware_deps,
            user_id=user_id,
            workspace_id=args.workspace_id,
            cwd=args.cwd,
            role=args.role,
            query=args.query.strip(),
            trigger=args.trigger,
            debug=True,
        )
        emit_json(result, args.output)
        return 0

    if args.command == "approve":
        user_id = args.user_id or config.default_user_id
        result = run_approve_middleware(
            config,
            middleware_deps,
            user_id=user_id,
            workspace_id=args.workspace_id,
            cwd=args.cwd,
            payload_files=args.payload_files,
            context_package_path=args.context_package,
            writeback=bool(args.writeback),
        )
        emit_json(result, args.output)
        return 0 if result.get("ok") else 1

    if args.command == "runstate":
        user_id = args.user_id or config.default_user_id
        workspace_id = resolve_workspace_id(config, args.cwd, args.workspace_id)
        state = load_state(config)
        bucket = normalize_run_state_bucket(
            state.get("workspaces", {}).get(build_state_key(user_id, workspace_id), {})
        )
        emit_json(bucket, args.output)
        return 0

    if args.command == "finalize":
        user_id = args.user_id or config.default_user_id
        result = run_finalize_middleware(
            config,
            middleware_deps,
            user_id=user_id,
            workspace_id=args.workspace_id,
            cwd=args.cwd,
            query=args.query.strip(),
        )
        emit_json(result, args.output)
        return 0 if result.get("ok") else 1

    if args.command == "compact":
        if not args.dry_run:
            emit_json(
                {
                    "ok": False,
                    "mode": "compact",
                    "error": "compact_currently_requires_dry_run_true",
                },
                args.output,
            )
            return 1
        user_id = args.user_id or config.default_user_id
        workspace_id = resolve_workspace_id(config, args.cwd, args.workspace_id)
        result = compact_memories_dry_run(
            config,
            user_id=user_id,
            workspace_id=workspace_id,
            target_scope=args.scope,
            memory_ids=args.memory_ids,
            limit=max(1, int(args.limit)),
            key=args.key,
        )
        emit_json(result, args.output)
        return 0 if result.get("ok") else 1

    if args.command == "backfill-confidence":
        user_id = args.user_id or config.default_user_id
        ids: list[str] | None = None
        if args.ids_file:
            ids = [
                line.strip()
                for line in Path(args.ids_file).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        result = backfill_confidence_scores(
            config,
            user_id=user_id,
            workspace_id=args.workspace_id,
            ids=ids,
            limit=args.limit,
            mode=args.mode,
        )
        emit_json(result, args.output)
        return 0 if result.get("ok") else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
