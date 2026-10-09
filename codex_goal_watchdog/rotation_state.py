"""Durable validation for recovery paths that may create a new thread."""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
import xml.etree.ElementTree as ET

from .app_server import (
    CodexAppServerClient,
    CodexAppServerError,
    GOAL_STATUSES,
    cached_native_goal_snapshot as _cached_native_goal,
    native_goal_snapshot as _native_goal_snapshot,
)
from .bindings import (
    load_session_binding,
    load_thread_handoff,
    save_binding_runtime_state,
    save_session_binding,
    save_thread_handoff,
)
from .sessions import (
    ThreadTelemetryTracker,
    find_active_cli_thread_id,
    find_thread_rollout_path,
)
from .launcher import tmux_pane_identity as _tmux_pane_identity
from .recovery import (
    PERSISTED_THREAD_ROTATION_REASONS,
    THREAD_ROTATION_RECOVERY_REASONS,
    RecoveryStep,
    build_thread_rotation_prompt,
)
from .tmux_control import (
    save_tmux_recovery_count as _save_tmux_recovery_count,
    save_tmux_successful_compactions as _save_tmux_successful_compactions,
)


PENDING_THREAD_ROTATION_OPTION = "@codex_pending_thread_rotation_count"
PENDING_THREAD_ROTATION_REASON_OPTION = "@codex_pending_thread_rotation_reason"
PENDING_THREAD_ROTATION_THREAD_OPTION = "@codex_pending_thread_rotation_thread_id"
GOAL_ACTIVE_OBJECTIVE_RE = re.compile(
    r"Goal active Objective:\s*(?P<objective>.*?)(?=\s+Time:\s*|"
    r"\n\s*(?:[›»•◦■]|Goal (?:active|blocked|stalled|paused))|$)",
    re.IGNORECASE | re.DOTALL,
)


def _goal_capture_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_goal_capture_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_goal_capture_text(item) for item in value)
    return ""


def find_rotation_goal_objective(thread_id: str) -> str | None:
    """Resolve a Goal objective from structured or legacy terminal rollout data."""
    path = find_thread_rollout_path(thread_id=thread_id)
    if path is None:
        return None
    structured: str | None = None
    screen: str | None = None
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = event.get("payload", {})
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") == "custom_tool_call_output":
                    match = GOAL_ACTIVE_OBJECTIVE_RE.search(
                        _goal_capture_text(payload.get("output"))
                    )
                    if match:
                        screen = re.sub(
                            r"\s+", " ", match.group("objective")
                        ).strip()
                if (
                    event.get("type") == "response_item"
                    and payload.get("type") == "message"
                    and payload.get("role") == "user"
                ):
                    for item in payload.get("content", []):
                        text = item.get("text") if isinstance(item, dict) else None
                        if not isinstance(text, str) or "<objective>" not in text:
                            continue
                        try:
                            context = ET.fromstring(text.strip())
                        except ET.ParseError:
                            continue
                        if context.get("source") == "goal":
                            value = context.findtext("objective")
                            if value and value.strip():
                                structured = value.strip()
    except OSError:
        return None
    return structured or screen


def _handoff_matches_rebound_thread(
    payload: dict[str, object],
    *,
    thread_id: str,
) -> bool:
    if payload.get("old_thread_id") == thread_id:
        return True
    telemetry = payload.get("telemetry")
    rebound_thread_id = (
        telemetry.get("rebound_thread_id")
        if isinstance(telemetry, dict)
        else None
    )
    if isinstance(rebound_thread_id, str):
        return rebound_thread_id == thread_id
    return False


def _cached_native_goal_for_thread(
    session: str,
    thread_id: str,
) -> dict[str, object] | None:
    snapshot = _cached_native_goal(session)
    loaded = load_thread_handoff(session)
    if snapshot is None or loaded is None:
        return None
    _path, payload = loaded
    if payload.get("reason") not in PERSISTED_THREAD_ROTATION_REASONS:
        return None
    source_thread_id = snapshot.get("source_thread_id")
    old_thread_id = payload.get("old_thread_id")
    telemetry = payload.get("telemetry")
    rebound_thread_id = (
        telemetry.get("rebound_thread_id")
        if isinstance(telemetry, dict)
        else None
    )
    if (
        source_thread_id == thread_id
        and (old_thread_id == thread_id or rebound_thread_id == thread_id)
    ):
        return snapshot
    if (
        source_thread_id == old_thread_id
        and rebound_thread_id == thread_id
    ):
        return {**snapshot, "source_thread_id": thread_id}
    return None


def write_thread_rotation_handoff(
    *, session: str, thread_id: str, cwd: Path, reason: str
) -> Path:
    telemetry = ThreadTelemetryTracker(thread_id=thread_id).snapshot()
    data = asdict(telemetry) if telemetry is not None else {}
    if data:
        data["rollout_path"] = str(data["rollout_path"])
        data.pop("latest_failure", None)
    native_goal: dict[str, object] | None = None
    try:
        with CodexAppServerClient() as app_server:
            native_goal = _native_goal_snapshot(
                app_server.get_goal(thread_id),
                source_thread_id=thread_id,
            )
    except (CodexAppServerError, OSError, TimeoutError):
        native_goal = None
    if native_goal is None:
        native_goal = _cached_native_goal_for_thread(session, thread_id)
    if native_goal is not None:
        data["native_goal"] = native_goal
    goal_objective = (
        native_goal.get("objective")
        if native_goal is not None
        else find_rotation_goal_objective(thread_id)
    )
    if not isinstance(goal_objective, str) or not goal_objective.strip():
        goal_objective = None
    return save_thread_handoff(
        session=session,
        thread_id=thread_id,
        cwd=cwd,
        reason=reason,
        goal_objective=goal_objective,
        telemetry=data,
    )


def wait_for_native_goal_state(
    target: str,
    *,
    timeout_seconds: float = 120,
    app_server_factory: Callable = CodexAppServerClient,
    sleeper=time.sleep,
    now=time.monotonic,
) -> str:
    binding = load_session_binding(target)
    if binding is None:
        raise RuntimeError(f"no thread binding exists for tmux session {target}")
    deadline = now() + max(0, timeout_seconds)
    states = {
        "active": "pursuing",
        "blocked": "blocked",
        "paused": "paused",
        "usageLimited": "usage_limited",
        "budgetLimited": "budget_limited",
        "complete": "achieved",
    }
    with app_server_factory() as app_server:
        while now() < deadline:
            goal = app_server.get_goal(binding.thread_id)
            if isinstance(goal, dict) and goal.get("status") in states:
                return states[goal["status"]]
            sleeper(0.5)
    raise TimeoutError(f"Codex thread {target} did not restore a native Goal")


def _goal_state_from_text(text: str) -> str | None:
    markers = (
        ("pursuing", "Pursuing goal"),
        ("pursuing", "Goal active Objective:"),
        ("blocked", "Goal blocked (/goal resume)"),
        ("stalled", "Goal stalled (/goal resume)"),
    )
    return max(
        ((text.rfind(marker), state) for state, marker in markers),
        default=(-1, None),
    )[1]


def _tmux_option_value(
    target: str,
    name: str,
    *,
    runner: Callable = subprocess.run,
) -> str:
    result = runner(
        ["tmux", "show-option", "-v", "-t", target, name],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def pending_thread_rotation_count(
    target: str,
    *,
    runner: Callable = subprocess.run,
) -> int | None:
    value = _tmux_option_value(
        target,
        PENDING_THREAD_ROTATION_OPTION,
        runner=runner,
    )
    if not value:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        return None


def pending_thread_rotation_reason(
    target: str,
    *,
    runner: Callable = subprocess.run,
) -> str:
    return _tmux_option_value(
        target,
        PENDING_THREAD_ROTATION_REASON_OPTION,
        runner=runner,
    )


def pending_thread_rotation_source_thread_id(
    target: str,
    *,
    runner: Callable = subprocess.run,
) -> str:
    return _tmux_option_value(
        target,
        PENDING_THREAD_ROTATION_THREAD_OPTION,
        runner=runner,
    )


def pending_thread_rotation_is_valid(
    target: str,
    *,
    thread_id: str | None = None,
    runner: Callable = subprocess.run,
) -> bool:
    count = pending_thread_rotation_count(target, runner=runner)
    if count is None or count <= 0:
        return False
    reason = pending_thread_rotation_reason(target, runner=runner)
    if reason not in PERSISTED_THREAD_ROTATION_REASONS:
        return False
    source_thread_id = pending_thread_rotation_source_thread_id(
        target,
        runner=runner,
    )
    if not source_thread_id:
        return False
    return not thread_id or source_thread_id == thread_id


def pending_thread_rotation_marker(
    target: str,
    *,
    thread_id: str | None = None,
    runner: Callable = subprocess.run,
) -> int | None:
    """Return a valid marker and clear legacy or mismatched state."""
    count = pending_thread_rotation_count(target, runner=runner)
    if count is None:
        return None
    if pending_thread_rotation_is_valid(
        target,
        thread_id=thread_id,
        runner=runner,
    ):
        return count
    clear_pending_thread_rotation(target)
    return None


def has_pending_thread_rotation(
    target: str,
    *,
    thread_id: str | None = None,
    runner: Callable = subprocess.run,
) -> bool:
    return pending_thread_rotation_marker(
        target,
        thread_id=thread_id,
        runner=runner,
    ) is not None


def pending_thread_rotation_prompt(
    target: str,
    *,
    thread_id: str,
    goal_state: str | None,
) -> str | None:
    """Build a prompt only from a supported handoff for the pinned thread."""
    loaded = load_thread_handoff(target)
    if loaded is None:
        return None
    path, payload = loaded
    reason = payload.get("reason")
    if reason not in PERSISTED_THREAD_ROTATION_REASONS:
        return None
    if payload.get("old_thread_id") != thread_id:
        # The binding is rebound as soon as the fresh CLI thread appears, but
        # the handoff remains authoritative until native Goal state is seen.
        binding = load_session_binding(target)
        if not (
            binding is not None
            and binding.thread_id == thread_id
            and binding.verification_pending
            and _handoff_matches_rebound_thread(
                payload,
                thread_id=thread_id,
            )
        ):
            return None
    telemetry = payload.get("telemetry")
    native_goal = (
        _native_goal_snapshot(
            telemetry.get("native_goal"),
            source_thread_id=(
                telemetry["native_goal"].get("source_thread_id", "")
                if isinstance(telemetry, dict)
                and isinstance(telemetry.get("native_goal"), dict)
                else ""
            ),
        )
        if isinstance(telemetry, dict)
        else None
    )
    objective = (
        native_goal.get("objective")
        if native_goal is not None
        else payload.get("goal_objective")
    )
    if not isinstance(objective, str) or not objective.strip():
        return None
    return build_thread_rotation_prompt(
        objective,
        resume_goal=(
            native_goal.get("status") == "active"
            if native_goal is not None
            else goal_state not in {"blocked", "stalled"}
        ),
        rotation_reason=reason,
        handoff_path=str(path),
    )


def pending_recovery_prompt(
    target: str,
    *,
    thread_id: str,
    goal_state: str | None,
    resume_prompt: str,
) -> str | None:
    binding = load_session_binding(target)
    loaded = load_thread_handoff(target)
    rotation_pending = has_pending_thread_rotation(
        target,
        thread_id=thread_id,
    )
    if (
        binding is not None
        and binding.verification_pending
        and loaded is not None
        and loaded[1].get("reason") in THREAD_ROTATION_RECOVERY_REASONS
    ):
        rotation_pending = True
    if rotation_pending:
        prompt = pending_thread_rotation_prompt(
            target,
            thread_id=thread_id,
            goal_state=goal_state,
        )
        if prompt is not None:
            return prompt
        if binding is not None and binding.verification_pending:
            return None
    if goal_state in {"paused", "usage_limited", "stalled"}:
        return "/goal resume"
    if binding is not None and binding.verification_pending:
        return resume_prompt
    return None


def restore_pending_native_goal(target: str, thread_id: str) -> bool:
    binding = load_session_binding(target)
    loaded = load_thread_handoff(target)
    if not (
        binding is not None
        and binding.thread_id == thread_id
        and binding.verification_pending
        and loaded is not None
        and loaded[1].get("reason") in THREAD_ROTATION_RECOVERY_REASONS
    ):
        return False
    restore_native_goal_for_thread(target, thread_id)
    return True


def restore_native_goal_for_thread(
    target: str,
    thread_id: str,
    *,
    status_override: str | None = None,
    origin: str = "automatic",
    require_bound: bool = True,
    app_server_factory: Callable = CodexAppServerClient,
) -> dict[str, object]:
    """Restore and read back the handoff's native Goal on the bound thread."""
    loaded = load_thread_handoff(target)
    if loaded is None:
        raise RuntimeError(f"no Goal handoff exists for tmux session {target}")
    _path, payload = loaded
    if payload.get("reason") not in THREAD_ROTATION_RECOVERY_REASONS:
        raise RuntimeError("handoff reason does not authorize thread Goal restore")
    binding = load_session_binding(target)
    if require_bound and (
        binding is None or binding.thread_id != thread_id
    ):
        raise RuntimeError("native Goal restore target is not the bound thread")
    if require_bound and binding is not None and not _handoff_matches_rebound_thread(
        payload,
        thread_id=thread_id,
    ):
        raise RuntimeError("Goal handoff does not match the current rebound thread")
    telemetry = payload.get("telemetry")
    snapshot = (
        _native_goal_snapshot(
            telemetry.get("native_goal"),
            source_thread_id=(
                telemetry["native_goal"].get("source_thread_id", "")
                if isinstance(telemetry, dict)
                and isinstance(telemetry.get("native_goal"), dict)
                else ""
            ),
        )
        if isinstance(telemetry, dict)
        else None
    )
    if snapshot is None:
        source_thread_id = payload.get("old_thread_id")
        if not isinstance(source_thread_id, str) or not source_thread_id:
            raise RuntimeError("Goal handoff has no native Goal source thread")
        try:
            with app_server_factory() as app_server:
                snapshot = _native_goal_snapshot(
                    app_server.get_goal(source_thread_id),
                    source_thread_id=source_thread_id,
                )
        except (CodexAppServerError, OSError, TimeoutError) as exc:
            raise RuntimeError("could not read native Goal from source thread") from exc
    if snapshot is None:
        raise RuntimeError("Goal handoff has no recoverable native Goal snapshot")

    objective = snapshot["objective"]
    status = status_override or snapshot["status"]
    token_budget = snapshot["token_budget"]
    if status not in GOAL_STATUSES:
        raise ValueError(f"unsupported restored native Goal status: {status}")
    try:
        with app_server_factory() as app_server:
            current = app_server.get_goal(thread_id)
            if not (
                isinstance(current, dict)
                and current.get("objective") == objective
                and current.get("status") == status
                and current.get("tokenBudget") == token_budget
            ):
                app_server.set_goal(
                    thread_id=thread_id,
                    objective=objective,
                    status=status,
                    token_budget=token_budget,
                    origin=origin,
                )
            restored = app_server.get_goal(thread_id)
    except (CodexAppServerError, OSError, TimeoutError) as exc:
        raise RuntimeError("native Goal restore failed") from exc
    if not (
        isinstance(restored, dict)
        and restored.get("threadId") == thread_id
        and restored.get("objective") == objective
        and restored.get("status") == status
        and restored.get("tokenBudget") == token_budget
    ):
        raise RuntimeError("native Goal readback did not match the handoff")
    return restored


def restore_native_goal_after_rotation(
    target: str,
    source_thread_id: str,
    *,
    timeout_seconds: float = 30,
    sleeper=time.sleep,
    now=time.monotonic,
    resolve_thread_id: Callable[[int, Path], str | None] | None = None,
) -> tuple[str, dict[str, object]]:
    """Wait for the fresh CLI rollout, bind it, and restore its native Goal."""
    deadline = now() + max(0, timeout_seconds)
    resolver = resolve_thread_id or (
        lambda pane_pid, cwd: find_active_cli_thread_id(
            pane_pid=pane_pid,
            cwd=cwd,
        )
    )
    while now() < deadline:
        identity = _tmux_pane_identity(target)
        if identity is not None:
            pane_pid, cwd = identity
            thread_id = resolver(pane_pid, cwd)
            if thread_id and thread_id != source_thread_id:
                return thread_id, restore_native_goal_for_thread(
                    target,
                    thread_id,
                    require_bound=False,
                )
        sleeper(0.25)
    raise TimeoutError(f"new Codex thread did not appear in tmux session {target}")


def complete_native_goal_rotation(
    target: str,
    source_thread_id: str,
    reason: str,
    handoff_path: str | None,
    execute_steps: Callable[[str, list], None],
    *,
    restore_thread_goal: Callable[
        [str, str], tuple[str, dict[str, object]]
    ] = restore_native_goal_after_rotation,
) -> tuple[str, dict[str, object]]:
    """Restore the Goal before submitting any continuation text."""
    thread_id, native_goal = restore_thread_goal(target, source_thread_id)
    if native_goal.get("status") == "active":
        prompt = build_thread_rotation_prompt(
            str(native_goal["objective"]),
            resume_goal=True,
            rotation_reason=reason,
            handoff_path=handoff_path,
        )
        execute_steps(target, [RecoveryStep("text", prompt)])
    return thread_id, native_goal


def set_pending_thread_rotation(
    target: str,
    count: int,
    *,
    reason: str,
    source_thread_id: str,
) -> bool:
    """Persist a new-thread marker only when its source and reason are safe."""
    if (
        reason not in PERSISTED_THREAD_ROTATION_REASONS
        or not source_thread_id
        or count <= 0
    ):
        clear_pending_thread_rotation(target)
        return False
    for option, value in (
        (PENDING_THREAD_ROTATION_REASON_OPTION, reason),
        (PENDING_THREAD_ROTATION_THREAD_OPTION, source_thread_id),
    ):
        subprocess.run(
            ["tmux", "set-option", "-t", target, option, value],
            check=True,
        )
    subprocess.run(
        [
            "tmux",
            "set-option",
            "-t",
            target,
            PENDING_THREAD_ROTATION_OPTION,
            str(max(0, count)),
        ],
        check=True,
    )
    return True


def clear_pending_thread_rotation(target: str) -> None:
    for option in (
        PENDING_THREAD_ROTATION_OPTION,
        PENDING_THREAD_ROTATION_REASON_OPTION,
        PENDING_THREAD_ROTATION_THREAD_OPTION,
    ):
        subprocess.run(
            ["tmux", "set-option", "-u", "-t", target, option],
            check=True,
        )


def _mark_rotation_handoff_rebound(
    target: str,
    *,
    old_thread_id: str,
    new_thread_id: str,
    cwd: Path,
) -> None:
    loaded = load_thread_handoff(target)
    if loaded is None:
        return
    _path, payload = loaded
    reason = payload.get("reason")
    if (
        payload.get("old_thread_id") != old_thread_id
        or reason not in PERSISTED_THREAD_ROTATION_REASONS
    ):
        return
    telemetry = payload.get("telemetry")
    data = dict(telemetry) if isinstance(telemetry, dict) else {}
    data["rebound_thread_id"] = new_thread_id
    save_thread_handoff(
        session=target,
        thread_id=old_thread_id,
        cwd=cwd,
        reason=reason,
        goal_objective=(
            payload.get("goal_objective")
            if isinstance(payload.get("goal_objective"), str)
            else None
        ),
        telemetry=data,
    )


def save_rebound_thread_id(target: str, thread_id: str) -> None:
    """Persist a discovered CLI thread and consume only a valid rotation."""
    previous_binding = load_session_binding(target)
    pending_rotation_count = pending_thread_rotation_marker(
        target,
        thread_id=(previous_binding.thread_id if previous_binding is not None else None),
    )
    verification_pending = (
        previous_binding.verification_pending
        if previous_binding is not None
        else None
    )
    verification_baseline = (
        previous_binding.verification_baseline
        if previous_binding is not None
        else None
    )
    bound_recovery_phase = (
        previous_binding.recovery_phase
        if previous_binding is not None
        else None
    )
    bound_recovery_not_before = (
        previous_binding.recovery_not_before
        if previous_binding is not None
        else None
    )
    bound_recovery_reason = (
        previous_binding.last_recovery_reason
        if previous_binding is not None
        else None
    )
    if pending_rotation_count is not None:
        verification_pending = True
    subprocess.run(
        ["tmux", "set-option", "-t", target, "@codex_thread_id", thread_id],
        check=True,
    )
    if pending_rotation_count is None:
        _save_tmux_recovery_count(target, 0)
        rebound_recovery_count = 0
    else:
        _save_tmux_recovery_count(target, pending_rotation_count)
        rebound_recovery_count = pending_rotation_count
        if previous_binding is not None:
            _mark_rotation_handoff_rebound(
                target,
                old_thread_id=previous_binding.thread_id,
                new_thread_id=thread_id,
                cwd=previous_binding.cwd,
            )
        clear_pending_thread_rotation(target)
    _save_tmux_successful_compactions(target, 0)
    pane_identity = _tmux_pane_identity(target)
    if pane_identity is None:
        return
    _, cwd = pane_identity
    has_recovery_phase = (
        bound_recovery_phase not in {None, "idle"}
        or bound_recovery_not_before
        or bound_recovery_reason
    )
    if has_recovery_phase:
        save_session_binding(
            session=target,
            thread_id=thread_id,
            cwd=cwd,
            verification_pending=verification_pending,
            verification_baseline=verification_baseline,
            recovery_phase=bound_recovery_phase,
            recovery_not_before=bound_recovery_not_before,
            last_recovery_reason=bound_recovery_reason,
        )
        save_binding_runtime_state(
            session=target,
            recovery_count=rebound_recovery_count,
            successful_compactions=0,
            verification_pending=verification_pending,
            verification_baseline=verification_baseline,
            recovery_phase=bound_recovery_phase,
            recovery_not_before=bound_recovery_not_before,
            last_recovery_reason=bound_recovery_reason,
        )
        return
    save_session_binding(
        session=target,
        thread_id=thread_id,
        cwd=cwd,
        verification_pending=verification_pending,
        verification_baseline=verification_baseline,
    )
    save_binding_runtime_state(
        session=target,
        recovery_count=rebound_recovery_count,
        successful_compactions=0,
        verification_pending=verification_pending,
        verification_baseline=verification_baseline,
    )
