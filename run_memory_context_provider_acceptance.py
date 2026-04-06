#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import main_agent_runtime as runtime
from memory_context_provider import build_context_bundle


REPORT_PATH = Path(__file__).resolve().parent / "runtime" / "acceptance" / "memory_context_provider_acceptance_report.json"


def stable_bundle_view(payload: dict) -> dict:
    debug = payload.get("debug") or {}
    semantic_backend = debug.get("semantic_backend") or {}
    return {
        "trigger": payload.get("trigger"),
        "context_blocks": payload.get("context_blocks"),
        "source_context_blocks": payload.get("source_context_blocks"),
        "stale_or_superseded": payload.get("stale_or_superseded"),
        "context_text": payload.get("context_text"),
        "context_token_estimate": payload.get("context_token_estimate"),
        "budget_profile": payload.get("budget_profile"),
        "debug": {
            "thresholds": debug.get("thresholds"),
            "budget": debug.get("budget"),
            "semantic_backend": {
                "enabled": semantic_backend.get("enabled"),
                "status": semantic_backend.get("status"),
                "backend": semantic_backend.get("backend"),
                "mode": semantic_backend.get("mode"),
            },
        },
    }


def main() -> int:
    config = runtime.load_config(runtime.CONFIG_PATH.resolve())
    deps = runtime.build_memory_context_provider_deps()
    cases = [
        {
            "name": "planner_workspace_switch",
            "kwargs": {
                "user_id": config.default_user_id,
                "workspace_id": "ollamashiyong",
                "role": "Planner",
                "query": "回到 ollamashiyong 工作区后继续当前治理任务",
                "trigger": "on_workspace_switch",
            },
        },
        {
            "name": "retriever_low_confidence",
            "kwargs": {
                "user_id": config.default_user_id,
                "workspace_id": "ollamashiyong",
                "role": "Retriever",
                "query": "未审批证据先留 Retrieved Facts",
                "trigger": "before_task_start",
            },
        },
    ]

    results = []
    for case in cases:
        direct = build_context_bundle(
            config,
            deps,
            context_override_blocks=runtime.CONTEXT_OVERRIDE_BLOCKS,
            role_allowed_blocks=runtime.ROLE_ALLOWED_BLOCKS,
            debug=True,
            **case["kwargs"],
        )
        wrapped = runtime.build_context_debug_package(config, **case["kwargs"])
        direct_view = stable_bundle_view(direct)
        wrapped_view = stable_bundle_view(wrapped)
        results.append(
            {
                "name": case["name"],
                "ok": direct_view == wrapped_view,
                "direct": direct_view,
                "wrapped": wrapped_view,
            }
        )

    report = {"ok": all(item["ok"] for item in results), "results": results}
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
