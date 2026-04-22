from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any


def build_excerpt(*, source: str, label: str, text: str, severity: str = "info") -> dict[str, str]:
    return {
        "source": source,
        "label": label,
        "text": text,
        "severity": severity,
    }


def default_result_contract(*, action: str) -> dict[str, Any]:
    return {
        "action": action,
        "decision": "pending",
        "failure_stage": None,
        "retryable": None,
        "stop_reason": None,
        "key_excerpts": [],
        "next_actions": [],
        "carry_forward_request": {},
    }


def default_interventions_summary(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "count": 0,
        "latest": None,
    }


def build_next_iteration_request(
    *,
    action: str,
    loop_context: dict[str, Any] | None,
    current_session_dir: Path | None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if loop_context is None or current_session_dir is None:
        return {}

    iteration = int(loop_context.get("iteration") or 1) + 1
    loop_request = {
        "goal_id": loop_context.get("goal_id"),
        "goal": loop_context.get("goal"),
        "prev_session": str(Path(current_session_dir).absolute()),
        "iteration": iteration,
        "max_iterations": loop_context.get("max_iterations"),
    }
    loop_request = {key: value for key, value in loop_request.items() if value not in {None, ""}}

    request: dict[str, Any] = {
        "action": action,
        "loop": loop_request,
    }
    if options:
        request["options"] = dict(options)
    return request


def build_result_contract(
    *,
    action: str,
    decision: str = "pending",
    failure_stage: str | None = None,
    retryable: bool | None = None,
    stop_reason: str | None = None,
    key_excerpts: list[dict[str, str]] | None = None,
    next_actions: list[dict[str, Any]] | None = None,
    loop_context: dict[str, Any] | None = None,
    current_session_dir: Path | None = None,
    carry_forward_action: str | None = None,
    carry_forward_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = default_result_contract(action=action)
    result["decision"] = decision
    result["failure_stage"] = failure_stage
    result["retryable"] = retryable
    result["stop_reason"] = stop_reason
    result["key_excerpts"] = list(key_excerpts or [])
    result["next_actions"] = list(next_actions or _default_next_actions(action=action, decision=decision))
    carry_forward_request = build_next_iteration_request(
        action=carry_forward_action or action,
        loop_context=loop_context,
        current_session_dir=current_session_dir,
        options=carry_forward_options,
    )
    if decision in {"continue", "blocked", "manual_required", "stalled"} and carry_forward_request:
        result["carry_forward_request"] = carry_forward_request
    return result


def build_intervention_record(
    *,
    kind: str,
    summary: str,
    details: str = "",
    files: list[str] | None = None,
    git_commit: str | None = None,
    expected_effect: str | None = None,
    related_session: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "summary": summary,
        "details": details,
        "files": list(files or []),
        "git_commit": git_commit,
        "expected_effect": expected_effect,
        "related_session": related_session,
        "metadata": dict(metadata or {}),
    }


def _default_next_actions(*, action: str, decision: str) -> list[dict[str, str]]:
    if decision == "blocked":
        return [{"action": action, "reason": "Unblock the missing prerequisite, then retry this action."}]
    if decision in {"continue", "manual_required", "stalled"}:
        return [{"action": action, "reason": "Retry this action in the next debug iteration."}]
    return []
