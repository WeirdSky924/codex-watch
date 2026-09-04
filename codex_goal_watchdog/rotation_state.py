"""Durable validation for recovery paths that may create a new thread."""

from __future__ import annotations

import subprocess
from collections.abc import Callable

from .bindings import (
    load_session_binding,
    load_thread_handoff,
    save_binding_runtime_state,
    save_session_binding,
)
from .launcher import tmux_pane_identity as _tmux_pane_identity
from .recovery import (
    PERSISTED_THREAD_ROTATION_REASONS,
    build_thread_rotation_prompt,
)
from .tmux_control import (
    save_tmux_recovery_count as _save_tmux_recovery_count,
    save_tmux_successful_compactions as _save_tmux_successful_compactions,
)


PENDING_THREAD_ROTATION_OPTION = "@codex_pending_thread_rotation_count"
PENDING_THREAD_ROTATION_REASON_OPTION = "@codex_pending_thread_rotation_reason"
PENDING_THREAD_ROTATION_THREAD_OPTION = "@codex_pending_thread_rotation_thread_id"


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
    if payload.get("old_thread_id") != thread_id:
        return None
    reason = payload.get("reason")
    if reason not in PERSISTED_THREAD_ROTATION_REASONS:
        return None
    objective = payload.get("goal_objective")
    return build_thread_rotation_prompt(
        objective if isinstance(objective, str) and objective else None,
        resume_goal=goal_state not in {"blocked", "stalled"},
        rotation_reason=reason,
        handoff_path=str(path),
    )


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
