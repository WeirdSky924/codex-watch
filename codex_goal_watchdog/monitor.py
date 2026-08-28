"""Monitor tmux pipe output and trigger Codex recovery."""

from __future__ import annotations

import codecs
import select
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO

from .bindings import (
    load_session_binding,
    save_binding_runtime_state,
    save_session_binding,
    save_thread_handoff,
)
from .recovery import (
    COMPACTION_RECOVERY_REASONS,
    THREAD_HEALTH_ROTATION_REASON,
    THREAD_ROTATION_RECOVERY_REASONS,
    RecoveryConfig,
    IncidentLogAggregator,
    build_recovery_steps,
    classify_recovery_message,
    classify_recovery_reason,
    thread_rotation_reason,
)
from .sessions import (
    ThreadTelemetry,
    ThreadTelemetryTracker,
    find_active_cli_thread_id,
    find_latest_goal_objective,
    find_latest_task_failure,
)
from .tmux_control import (
    capture_update_prompt_version,
    claim_tmux_recovery_incident_id as _claim_tmux_recovery_incident_id,
    clear_pending_thread_rotation as _clear_pending_thread_rotation,
    execute_steps,
    goal_state_from_text,
    handle_goal_prompt,
    normalize_terminal_text,
    paused_goal_picker_visible,
    pending_thread_rotation_count as _pending_thread_rotation_count,
    save_tmux_recovery_count as _save_tmux_recovery_count_impl,
    save_tmux_successful_compactions as _save_tmux_successful_compactions_impl,
    set_pending_thread_rotation as _set_pending_thread_rotation,
    tmux_recovery_count as _tmux_recovery_count_impl,
    tmux_recovery_incident_id as _tmux_recovery_incident_id,
    tmux_successful_compactions as _tmux_successful_compactions,
    resume_interrupted_update as _resume_interrupted_update,
    run_codex_update as _run_codex_update,
    update_prompt_version,
)
from .tmux_control import recovery_goal_state_on_screen
from .launcher import tmux_pane_identity as _tmux_pane_identity


ROLLING_BUFFER_SIZE = 8192
GOAL_RESUME_STATUS_MARKERS = (
    "Goal paused (/goal resume)",
    "Goal hit usage limits (/goal resume)",
)
GOAL_RESUME_RETRY_SECONDS = 10
RECOVERABLE_GOAL_STATES = {"pursuing", "blocked", "stalled"}
MONITOR_TICK = object()


def recovery_allowed_for_goal_state(state: str | None) -> bool:
    return state in RECOVERABLE_GOAL_STATES


def recovery_allowed_for_goal(value: str) -> bool:
    return recovery_allowed_for_goal_state(goal_state_from_text(value))


def recovery_incident_for_thread(thread_id: str) -> tuple[str, str] | None:
    failure = find_latest_task_failure(thread_id=thread_id)
    if failure is None:
        return None
    reason = classify_recovery_message(failure.message)
    if reason is None:
        return None
    return failure.incident_id, reason


def iter_decoded_chunks(
    stream: BinaryIO,
    *,
    chunk_size: int = 4096,
    idle_seconds: float | None = None,
) -> Iterable[str | object]:
    """Yield available terminal bytes without waiting for a newline."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    read_chunk = getattr(stream, "read1", stream.read)
    selectable = False
    if idle_seconds is not None and idle_seconds > 0:
        try:
            stream.fileno()
            selectable = True
        except (AttributeError, OSError):
            selectable = False
    while True:
        if selectable:
            ready, _, _ = select.select([stream], [], [], idle_seconds)
            if not ready:
                yield MONITOR_TICK
                continue
        chunk = read_chunk(chunk_size)
        if not chunk:
            break
        decoded = decoder.decode(chunk)
        if decoded:
            yield decoded
    tail = decoder.decode(b"", final=True)
    if tail:
        yield tail


def run_monitor(
    *,
    lines: Iterable[str | object],
    target: str,
    config: RecoveryConfig,
    now: Callable[[], float] = time.time,
    execute: Callable[[str, list], None] | None = None,
    resume_goal: Callable[[str], None] | None = None,
    update_codex: Callable[[str, str], None] | None = None,
    log: Callable[[str], None] | None = None,
    initial_recovery_count: int = 0,
    save_recovery_count: Callable[[int], None] | None = None,
    resolve_thread_id: Callable[[str], str | None] | None = None,
    save_thread_id: Callable[[str], None] | None = None,
    resolve_recovery_incident: (
        Callable[[str], tuple[str, str] | None] | None
    ) = None,
    initial_recovery_incident_id: str = "",
    save_recovery_incident_id: Callable[[str], None] | None = None,
    claim_recovery_incident_id: Callable[[str], bool] | None = None,
    resolve_goal_objective: Callable[[str], str | None] | None = None,
    mark_thread_rotation: Callable[[int], None] | None = None,
    verify_recovery: Callable[[str], bool] | None = None,
    initial_successful_compactions: int = 0,
    save_successful_compactions: Callable[[int], None] | None = None,
    resolve_thread_telemetry: Callable[[str], ThreadTelemetry | None] | None = None,
    write_thread_handoff: Callable[..., Path] | None = None,
    resolve_goal_state: Callable[[str], str | None] | None = None,
    initial_verification_pending: bool = False,
    initial_verification_baseline: int = 0,
    save_verification_state: Callable[[bool, int], None] | None = None,
) -> None:
    from .recovery import RecoveryController

    controller = RecoveryController(
        config,
        initial_recovery_count=initial_recovery_count,
    )
    emit = log or (lambda message: print(message, flush=True))

    def default_execute(tmux_target: str, steps: list) -> None:
        execute_steps(tmux_target, steps)

    def default_resume_goal(tmux_target: str) -> None:
        handle_goal_prompt(
            tmux_target,
            action="resume",
            prompt="",
            timeout_seconds=0,
            send_fallback_prompt=False,
        )

    def default_update_codex(tmux_target: str, expected_version: str) -> None:
        visible_version = capture_update_prompt_version(tmux_target)
        if visible_version is None:
            return
        _run_codex_update(
            tmux_target,
            config,
            visible_version,
            resume_goal=latest_goal_state not in {"blocked", "stalled"},
        )

    run_execute = execute or default_execute
    run_resume_goal = resume_goal or default_resume_goal
    run_update_codex = update_codex or default_update_codex
    rolling_output = ""
    last_goal_resume_at: float | None = None
    last_health_check_at: float | None = None
    latest_goal_state: str | None = None
    handled_incident_ids: dict[str, None] = (
        {initial_recovery_incident_id: None}
        if initial_recovery_incident_id
        else {}
    )
    preserve_recovery_count_on_rebind = False
    awaiting_verified_success = initial_verification_pending
    rotation_pending = False
    successful_compactions = max(0, initial_successful_compactions)
    incident_logs = IncidentLogAggregator(emit)
    verified_event_baselines: dict[str, int] = (
        {config.thread_id: max(0, initial_verification_baseline)}
        if initial_verification_pending
        else {}
    )

    def persist_recovery_count() -> None:
        if save_recovery_count is not None:
            save_recovery_count(controller.recovery_count)

    def reset_after_verified_success() -> None:
        controller.reset_after_verified_success()
        persist_recovery_count()
        if save_verification_state is not None:
            save_verification_state(False, 0)

    def mark_verified_progress_baseline(thread_id: str) -> None:
        if resolve_thread_telemetry is None:
            return
        telemetry = resolve_thread_telemetry(thread_id)
        if telemetry is not None:
            verified_event_baselines[thread_id] = telemetry.verified_event_count
            if save_verification_state is not None:
                save_verification_state(True, telemetry.verified_event_count)

    def rollout_has_verified_progress(thread_id: str) -> bool:
        if resolve_thread_telemetry is None:
            return False
        telemetry = resolve_thread_telemetry(thread_id)
        if telemetry is None:
            return False
        baseline = verified_event_baselines.get(thread_id)
        if baseline is None:
            verified_event_baselines[thread_id] = telemetry.verified_event_count
            return False
        return telemetry.verified_event_count > baseline

    def telemetry_has_verified_progress(thread_id: str) -> bool:
        if verify_recovery is not None:
            return verify_recovery(target)
        return rollout_has_verified_progress(thread_id)

    def create_handoff(
        *,
        reason: str,
        telemetry: ThreadTelemetry | None,
    ) -> str | None:
        if write_thread_handoff is None:
            return None
        objective = (
            resolve_goal_objective(config.thread_id)
            if resolve_goal_objective is not None
            else None
        )
        metrics = {}
        if telemetry is not None:
            metrics = {
                "rollout_path": str(telemetry.rollout_path),
                "rollout_bytes": telemetry.rollout_bytes,
                "total_tokens": telemetry.total_tokens,
                "context_tokens": telemetry.context_tokens,
                "context_window": telemetry.context_window,
                "compaction_count": telemetry.compaction_count,
                "tokens_at_last_progress": telemetry.tokens_at_last_progress,
                "last_event_at": telemetry.last_event_at,
                "last_progress_at": telemetry.last_progress_at,
                "turn_active": telemetry.turn_active,
                "repeated_content_count": telemetry.repeated_content_count,
                "repeated_command_count": telemetry.repeated_command_count,
                "repeated_content_signature": telemetry.repeated_content_signature,
                "repeated_command_signature": telemetry.repeated_command_signature,
            }
        return str(
            write_thread_handoff(
                session=target,
                thread_id=config.thread_id,
                reason=reason,
                goal_objective=objective,
                telemetry=metrics,
            )
        )

    def execute_rotation(
        *,
        detail: str,
        goal_state: str | None,
        telemetry: ThreadTelemetry | None,
        increment_attempt: bool,
        observed_at: float | None = None,
    ) -> bool:
        nonlocal preserve_recovery_count_on_rebind, rotation_pending
        nonlocal awaiting_verified_success
        if increment_attempt:
            event = controller.begin(
                reason=THREAD_HEALTH_ROTATION_REASON,
                now=observed_at if observed_at is not None else now(),
                line=detail,
            )
            if event is None:
                return False
            persist_recovery_count()
        handoff_path = create_handoff(reason=detail, telemetry=telemetry)
        preserve_recovery_count_on_rebind = True
        rotation_pending = True
        awaiting_verified_success = True
        if mark_thread_rotation is not None:
            mark_thread_rotation(controller.recovery_count)
        mark_verified_progress_baseline(config.thread_id)
        incident_logs.flush()
        emit(
            "[codex-goal-watchdog] rotating oversized or unhealthy thread: "
            f"{detail}; recovery #{controller.recovery_count}"
        )
        run_execute(
            target,
            build_recovery_steps(
                config,
                reason=THREAD_HEALTH_ROTATION_REASON,
                recovery_attempt=controller.recovery_count,
                resume_goal=goal_state != "blocked",
                resume_stalled_goal=goal_state == "stalled",
                goal_objective=(
                    resolve_goal_objective(config.thread_id)
                    if resolve_goal_objective is not None
                    else None
                ),
                handoff_path=handoff_path,
                rotation_detail=detail,
            ),
        )
        return True

    def check_thread_health(
        *,
        observed_at: float,
        goal_state: str | None,
        force: bool,
    ) -> bool:
        nonlocal last_health_check_at
        if resolve_thread_telemetry is None or rotation_pending:
            return False
        if not force:
            if last_health_check_at is None:
                last_health_check_at = observed_at
                return False
            if (
                observed_at - last_health_check_at
                < config.thread_health_poll_seconds
            ):
                return False
        last_health_check_at = observed_at
        effective_state = goal_state
        if effective_state is None and resolve_goal_state is not None:
            effective_state = resolve_goal_state(target)
        if not recovery_allowed_for_goal_state(effective_state):
            return False
        telemetry = resolve_thread_telemetry(config.thread_id)
        if telemetry is None:
            return False
        progress_tokens = telemetry.total_tokens
        recent_event = (
            telemetry.last_event_at > 0
            and observed_at - telemetry.last_event_at
            < max(1, config.thread_health_poll_seconds)
        )
        if telemetry.turn_active or recent_event:
            progress_tokens = telemetry.tokens_at_last_progress
        detail = thread_rotation_reason(
            config,
            compaction_count=max(
                successful_compactions,
                telemetry.compaction_count,
            ),
            rollout_bytes=telemetry.rollout_bytes,
            context_tokens=telemetry.context_tokens,
            # A long active turn is work in progress, not a stalled thread.
            # Context usage is owned by Codex; only watchdog-owned health
            # signals are evaluated here.
            total_tokens=progress_tokens,
            tokens_at_last_progress=telemetry.tokens_at_last_progress,
            last_event_age_seconds=max(0, observed_at - telemetry.last_event_at),
            repeated_content_count=telemetry.repeated_content_count,
            repeated_command_count=telemetry.repeated_command_count,
        )
        if detail is None:
            return False
        return execute_rotation(
            detail=detail,
            goal_state=effective_state,
            telemetry=telemetry,
            increment_attempt=True,
            observed_at=observed_at,
        )

    for line in lines:
        if line is MONITOR_TICK:
            observed_at = now()
            check_thread_health(
                observed_at=observed_at,
                goal_state=latest_goal_state,
                force=True,
            )
            continue
        if not isinstance(line, str):
            continue
        resolved_thread_id = (
            resolve_thread_id(target) if resolve_thread_id is not None else None
        )
        if resolved_thread_id and resolved_thread_id != config.thread_id:
            rebind_recovery_count = (
                controller.recovery_count
                if preserve_recovery_count_on_rebind
                else 0
            )
            config = replace(config, thread_id=resolved_thread_id)
            controller = RecoveryController(
                config,
                initial_recovery_count=rebind_recovery_count,
            )
            rolling_output = ""
            last_goal_resume_at = None
            last_health_check_at = None
            latest_goal_state = None
            handled_incident_ids.clear()
            if save_thread_id is not None:
                save_thread_id(resolved_thread_id)
            if (
                preserve_recovery_count_on_rebind
                and save_recovery_count is not None
            ):
                save_recovery_count(rebind_recovery_count)
            preserve_recovery_count_on_rebind = False
            awaiting_verified_success = rebind_recovery_count > 0
            rotation_pending = False
            if awaiting_verified_success:
                mark_verified_progress_baseline(resolved_thread_id)
            elif save_verification_state is not None:
                save_verification_state(False, 0)
            successful_compactions = 0
            if save_successful_compactions is not None:
                save_successful_compactions(0)
            emit(
                "[codex-goal-watchdog] rebound active thread: "
                f"{resolved_thread_id}"
            )
        normalized_line = normalize_terminal_text(line)
        line_goal_state = goal_state_from_text(normalized_line)
        if line_goal_state is not None:
            previous_goal_state = latest_goal_state
            latest_goal_state = line_goal_state
            if line_goal_state == "blocked" and previous_goal_state != "blocked":
                emit(
                    "[codex-goal-watchdog] goal blocked; "
                    "waiting for manual /goal resume"
                )
            if (
                awaiting_verified_success
                and line_goal_state in RECOVERABLE_GOAL_STATES
                and rollout_has_verified_progress(config.thread_id)
            ):
                reset_after_verified_success()
                awaiting_verified_success = False
                if save_verification_state is not None:
                    save_verification_state(False, 0)
                emit(
                    "[codex-goal-watchdog] rotated thread verified; "
                    "recovery count reset"
                )
        rolling_output = normalize_terminal_text(f"{rolling_output} {normalized_line}")
        rolling_output = rolling_output[-ROLLING_BUFFER_SIZE:]
        observed_at = now()
        if check_thread_health(
            observed_at=observed_at,
            goal_state=latest_goal_state,
            force=False,
        ):
            rolling_output = ""
            continue
        recovery_reason = classify_recovery_reason(rolling_output)
        if recovery_reason is not None and resolve_recovery_incident is not None:
            incident = resolve_recovery_incident(config.thread_id)
            if incident is None or incident[1] != recovery_reason:
                incident_logs.record(
                    key=f"unmatched:{recovery_reason}",
                    first=(
                        "[codex-goal-watchdog] ignored terminal error without "
                        f"matching rollout event: {recovery_reason}"
                    ),
                    summary=(
                        "[codex-goal-watchdog] unmatched terminal error aggregate: "
                        f"{recovery_reason}"
                    ),
                )
                rolling_output = ""
                continue
            incident_id, _ = incident
            if incident_id in handled_incident_ids:
                incident_logs.record(
                    key=f"redrawn:{incident_id}",
                    first=(
                        "[codex-goal-watchdog] ignored redrawn fatal event: "
                        f"{incident_id}"
                    ),
                    summary=(
                        "[codex-goal-watchdog] fatal redraw aggregate: "
                        f"{incident_id}"
                    ),
                )
                rolling_output = ""
                continue
            handled_incident_ids[incident_id] = None
            if len(handled_incident_ids) > 256:
                handled_incident_ids.pop(next(iter(handled_incident_ids)))
            if (
                claim_recovery_incident_id is not None
                and not claim_recovery_incident_id(incident_id)
            ):
                incident_logs.record(
                    key=f"claimed:{incident_id}",
                    first=(
                        "[codex-goal-watchdog] ignored fatal incident claimed by "
                        f"another recovery owner: {incident_id}"
                    ),
                    summary=(
                        "[codex-goal-watchdog] claimed incident aggregate: "
                        f"{incident_id}"
                    ),
                )
                rolling_output = ""
                continue
            if (
                claim_recovery_incident_id is None
                and save_recovery_incident_id is not None
            ):
                save_recovery_incident_id(incident_id)
        rolling_goal_state = goal_state_from_text(rolling_output)
        effective_goal_state = rolling_goal_state or latest_goal_state
        if recovery_reason is not None and not recovery_allowed_for_goal_state(
            effective_goal_state
        ):
            emit(
                "[codex-goal-watchdog] suppressed recovery outside active "
                f"or stalled/blocked goal: {recovery_reason}"
            )
            rolling_output = ""
            continue
        event = controller.observe(rolling_output, now=observed_at)
        if event is not None:
            rolling_output = ""
            incident_logs.flush()
            emit(
                f"[codex-goal-watchdog] recovery #{controller.recovery_count}: "
                f"{event.reason}"
            )
            persist_recovery_count()
            goal_objective = (
                resolve_goal_objective(config.thread_id)
                if event.reason == "upstream_access_denied"
                and resolve_goal_objective is not None
                else None
            )
            if event.reason in THREAD_ROTATION_RECOVERY_REASONS:
                execute_rotation(
                    detail=event.reason,
                    goal_state=effective_goal_state,
                    telemetry=(
                        resolve_thread_telemetry(config.thread_id)
                        if resolve_thread_telemetry is not None
                        else None
                    ),
                    increment_attempt=False,
                )
                continue
            try:
                mark_verified_progress_baseline(config.thread_id)
                awaiting_verified_success = True
                if (
                    save_verification_state is not None
                    and config.thread_id in verified_event_baselines
                ):
                    save_verification_state(
                        True,
                        verified_event_baselines[config.thread_id],
                    )
                run_execute(
                    target,
                    build_recovery_steps(
                        config,
                        reason=event.reason,
                        recovery_attempt=controller.recovery_count,
                        resume_goal=effective_goal_state != "blocked",
                        resume_stalled_goal=effective_goal_state == "stalled",
                        goal_objective=goal_objective,
                    ),
                )
            except TimeoutError:
                if event.reason not in COMPACTION_RECOVERY_REASONS:
                    raise
                execute_rotation(
                    detail="compaction_timeout",
                    goal_state=effective_goal_state,
                    telemetry=(
                        resolve_thread_telemetry(config.thread_id)
                        if resolve_thread_telemetry is not None
                        else None
                    ),
                    increment_attempt=False,
                )
                continue
            if event.reason in COMPACTION_RECOVERY_REASONS:
                successful_compactions += 1
                if save_successful_compactions is not None:
                    save_successful_compactions(successful_compactions)
            if telemetry_has_verified_progress(config.thread_id):
                reset_after_verified_success()
                awaiting_verified_success = False
            continue

        if (
            awaiting_verified_success
            and rollout_has_verified_progress(config.thread_id)
        ):
            reset_after_verified_success()
            awaiting_verified_success = False
            emit(
                "[codex-goal-watchdog] recovery verified by rollout progress; "
                "recovery count reset"
            )

        expected_update_version = update_prompt_version(rolling_output)
        if expected_update_version is not None:
            emit(
                "[codex-goal-watchdog] installing Codex update: "
                f"target={expected_update_version}"
            )
            run_update_codex(target, expected_update_version)
            rolling_output = ""
            continue

        goal_resume_visible = (
            effective_goal_state not in {"blocked", "stalled"}
            and (
                paused_goal_picker_visible(rolling_output)
                or any(
                    marker in rolling_output for marker in GOAL_RESUME_STATUS_MARKERS
                )
            )
        )
        retry_ready = (
            last_goal_resume_at is None
            or observed_at - last_goal_resume_at >= GOAL_RESUME_RETRY_SECONDS
        )
        if goal_resume_visible and retry_ready:
            telemetry = (
                resolve_thread_telemetry(config.thread_id)
                if resolve_thread_telemetry is not None
                else None
            )
            if telemetry is not None and telemetry.turn_active:
                rolling_output = ""
                continue
            emit("[codex-goal-watchdog] resuming paused goal")
            run_resume_goal(target)
            last_goal_resume_at = observed_at
            rolling_output = ""
    incident_logs.flush()


def _tmux_recovery_count(target: str) -> int:
    return _tmux_recovery_count_impl(target)


def _save_tmux_recovery_count(target: str, count: int) -> None:
    _save_tmux_recovery_count_impl(target, count)


def _save_tmux_successful_compactions(target: str, count: int) -> None:
    _save_tmux_successful_compactions_impl(target, count)


def _save_tmux_thread_id(target: str, thread_id: str) -> None:
    pending_rotation_count = _pending_thread_rotation_count(target)
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
        _clear_pending_thread_rotation(target)
    _save_tmux_successful_compactions(target, 0)
    pane_identity = _tmux_pane_identity(target)
    if pane_identity is not None:
        _, cwd = pane_identity
        save_session_binding(
            session=target,
            thread_id=thread_id,
            cwd=cwd,
        )
        save_binding_runtime_state(
            session=target,
            recovery_count=rebound_recovery_count,
            successful_compactions=0,
        )


def monitor_stdin(target: str, config: RecoveryConfig) -> None:
    print(f"[codex-goal-watchdog] monitor started: target={target}", flush=True)
    monitor_started_at = time.time()
    pane_identity = _tmux_pane_identity(target)
    telemetry_trackers: dict[str, ThreadTelemetryTracker] = {}
    binding = load_session_binding(target)
    initial_verification_pending = bool(
        binding is not None and binding.thread_id == config.thread_id
        and binding.verification_pending
    )
    initial_verification_baseline = (
        binding.verification_baseline
        if initial_verification_pending and binding is not None
        else 0
    )

    def resolve_thread_id(_target: str) -> str | None:
        if pane_identity is None:
            return None
        pane_pid, cwd = pane_identity
        return find_active_cli_thread_id(pane_pid=pane_pid, cwd=cwd)

    def resolve_thread_telemetry(thread_id: str) -> ThreadTelemetry | None:
        tracker = telemetry_trackers.get(thread_id)
        if tracker is None:
            tracker = ThreadTelemetryTracker(
                thread_id=thread_id,
                repetition_started_at=monitor_started_at,
            )
            telemetry_trackers[thread_id] = tracker
        return tracker.snapshot()

    def resolve_incremental_recovery_incident(
        thread_id: str,
    ) -> tuple[str, str] | None:
        telemetry = resolve_thread_telemetry(thread_id)
        if telemetry is None or telemetry.latest_failure is None:
            return None
        reason = classify_recovery_message(telemetry.latest_failure.message)
        if reason is None:
            return None
        return telemetry.latest_failure.incident_id, reason

    def write_handoff(**kwargs) -> Path:
        cwd = pane_identity[1] if pane_identity is not None else Path.cwd()
        return save_thread_handoff(cwd=cwd, **kwargs)

    def save_verification_state(pending: bool, baseline: int) -> None:
        save_binding_runtime_state(
            session=target,
            recovery_count=_tmux_recovery_count(target),
            successful_compactions=_tmux_successful_compactions(target),
            verification_pending=pending,
            verification_baseline=baseline,
        )

    active_thread_id = resolve_thread_id(target)
    if active_thread_id and active_thread_id != config.thread_id:
        config = replace(config, thread_id=active_thread_id)
        _save_tmux_thread_id(target, active_thread_id)
        print(
            "[codex-goal-watchdog] rebound active thread: "
            f"{active_thread_id}",
            flush=True,
        )
    _resume_interrupted_update(target, config)
    initial_incident_id = _tmux_recovery_incident_id(target)
    if not initial_incident_id:
        current_incident = resolve_incremental_recovery_incident(config.thread_id)
        if current_incident is not None:
            initial_incident_id = current_incident[0]
            _claim_tmux_recovery_incident_id(target, initial_incident_id)
    run_monitor(
        lines=iter_decoded_chunks(
            sys.stdin.buffer,
            idle_seconds=config.thread_health_poll_seconds,
        ),
        target=target,
        config=config,
        initial_recovery_count=_tmux_recovery_count(target),
        save_recovery_count=lambda count: _save_tmux_recovery_count(target, count),
        initial_successful_compactions=_tmux_successful_compactions(target),
        save_successful_compactions=lambda count: (
            _save_tmux_successful_compactions(target, count)
        ),
        resolve_thread_id=resolve_thread_id,
        save_thread_id=lambda thread_id: _save_tmux_thread_id(target, thread_id),
        resolve_recovery_incident=resolve_incremental_recovery_incident,
        initial_recovery_incident_id=initial_incident_id,
        claim_recovery_incident_id=lambda incident_id: (
            _claim_tmux_recovery_incident_id(target, incident_id)
        ),
        resolve_goal_objective=lambda thread_id: find_latest_goal_objective(
            thread_id=thread_id
        ),
        mark_thread_rotation=lambda count: _set_pending_thread_rotation(
            target,
            count,
        ),
        resolve_thread_telemetry=resolve_thread_telemetry,
        write_thread_handoff=write_handoff,
        resolve_goal_state=recovery_goal_state_on_screen,
        initial_verification_pending=initial_verification_pending,
        initial_verification_baseline=initial_verification_baseline,
        save_verification_state=save_verification_state,
    )
