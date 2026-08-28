"""Independent supervisor for the tmux output monitor."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from .launcher import DANGEROUS_BYPASS_ARG, tmux_session_exists
from .monitor import (
    _claim_tmux_recovery_incident_id,
    _set_pending_thread_rotation,
    _tmux_successful_compactions,
    normalize_terminal_text,
    recovery_incident_for_thread,
    recovery_allowed_for_goal,
    recovery_goal_state_on_screen,
)
from .paths import default_log_path
from .bindings import save_binding_runtime_state
from .recovery import (
    DEFAULT_RESUME_PROMPT,
    RecoveryConfig,
    build_post_update_restart_steps,
    build_recovery_steps,
    classify_recovery_reason,
)
from .sessions import ThreadTelemetryTracker, find_latest_goal_objective
from .tmux_control import (
    LAST_RECOVERY_INCIDENT_OPTION,
    PENDING_UPDATE_OPTION,
    execute_steps,
    monitor_pipe_command,
)


UPDATE_SUCCESS_MARKER = "Update ran successfully! Please restart Codex."
SHELL_COMMANDS = {"bash", "zsh", "sh", "fish"}


def guard_once(
    *,
    session_exists: Callable[[], bool],
    pipe_active: Callable[[], bool],
    stalled_screen: Callable[[], bool],
    recover: Callable[[], None],
    attach_monitor: Callable[[], None],
    update_restart_needed: Callable[[], bool] | None = None,
    restart_after_update: Callable[[], None] | None = None,
    inspect_active_screen: bool = True,
) -> str:
    if not session_exists():
        return "session_missing"
    if update_restart_needed is not None and update_restart_needed():
        if restart_after_update is None:
            raise ValueError("update restart callback is required")
        restart_after_update()
        return "restarted_after_update"
    monitor_is_active = pipe_active()
    if monitor_is_active and not inspect_active_screen:
        return "healthy"
    if stalled_screen():
        recover()
        if monitor_is_active:
            return "recovered"
        attach_monitor()
        return "recovered_and_reattached"
    if monitor_is_active:
        return "healthy"
    attach_monitor()
    return "reattached"


def _tmux_option(session: str, name: str, default: str = "") -> str:
    result = subprocess.run(
        ["tmux", "show-option", "-v", "-t", session, name],
        capture_output=True,
        text=True,
        check=False,
    )
    value = result.stdout.strip() if result.returncode == 0 else ""
    return value or default


def _next_recovery_attempt(
    session: str,
    *,
    option_getter: Callable[[str, str, str], str] = _tmux_option,
    runner=subprocess.run,
    persist_count: Callable[[str, int], object] | None = None,
) -> int:
    try:
        current = max(
            0,
            int(option_getter(session, "@codex_recovery_count", "0")),
        )
    except ValueError:
        current = 0
    attempt = current + 1
    runner(
        [
            "tmux",
            "set-option",
            "-t",
            session,
            "@codex_recovery_count",
            str(attempt),
        ],
        check=True,
    )
    if persist_count is None:
        persist_count = lambda target, count: save_binding_runtime_state(
            session=target,
            recovery_count=count,
            successful_compactions=_tmux_successful_compactions(target),
        )
    persist_count(session, attempt)
    return attempt


def _mark_verification_pending(session: str, config: RecoveryConfig) -> None:
    telemetry = ThreadTelemetryTracker(thread_id=config.thread_id).snapshot()
    baseline = telemetry.verified_event_count if telemetry is not None else 0
    compactions = telemetry.compaction_count if telemetry is not None else 0
    save_binding_runtime_state(
        session=session,
        recovery_count=_tmux_option_int(session, "@codex_recovery_count", 0),
        successful_compactions=max(
            _tmux_successful_compactions(session),
            compactions,
        ),
        verification_pending=True,
        verification_baseline=baseline,
    )


def _tmux_option_int(session: str, name: str, default: int) -> int:
    try:
        return max(0, int(_tmux_option(session, name, str(default))))
    except (TypeError, ValueError):
        return max(0, default)


def _pipe_active(session: str) -> bool:
    result = subprocess.run(
        ["tmux", "list-panes", "-t", session, "-F", "#{pane_pipe}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.splitlines()[:1] == ["1"]


def _update_completed_on_shell(
    session: str, *, runner=subprocess.run
) -> bool:
    pane_result = runner(
        [
            "tmux",
            "display-message",
            "-p",
            "-t",
            session,
            "#{pane_current_command}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if (
        pane_result.returncode != 0
        or pane_result.stdout.strip() not in SHELL_COMMANDS
    ):
        return False
    screen_result = runner(
        ["tmux", "capture-pane", "-p", "-t", session],
        capture_output=True,
        text=True,
        check=False,
    )
    return screen_result.returncode == 0 and UPDATE_SUCCESS_MARKER in (
        normalize_terminal_text(screen_result.stdout)
    )


def _guardian_update_restart_needed(
    session: str,
    *,
    option_getter: Callable[[str, str, str], str] = _tmux_option,
    completion_checker: Callable[[str], bool] = _update_completed_on_shell,
) -> bool:
    return not option_getter(session, PENDING_UPDATE_OPTION, "") and (
        completion_checker(session)
    )


def _recovery_reason_on_screen(session: str, *, runner=subprocess.run) -> str | None:
    result = runner(
        ["tmux", "capture-pane", "-p", "-t", session],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    screen = normalize_terminal_text(result.stdout)
    if not recovery_allowed_for_goal(screen):
        return None
    return classify_recovery_reason(screen)


def _unhandled_recovery_incident_on_screen(
    session: str,
    *,
    thread_id: str,
    screen_reason: Callable[[str], str | None] = _recovery_reason_on_screen,
    incident_resolver: Callable[
        [str], tuple[str, str] | None
    ] = recovery_incident_for_thread,
    option_getter: Callable[[str, str, str], str] = _tmux_option,
) -> tuple[str, str] | None:
    reason = screen_reason(session)
    if reason is None:
        return None
    incident = incident_resolver(thread_id)
    if incident is None or incident[1] != reason:
        return None
    if incident[0] == option_getter(
        session,
        LAST_RECOVERY_INCIDENT_OPTION,
        "",
    ):
        return None
    return incident


def _recovery_config(
    session: str,
    *,
    option_getter: Callable[[str, str, str], str] = _tmux_option,
) -> RecoveryConfig:
    codex_args_json = option_getter(
        session,
        "@codex_args_json",
        json.dumps([DANGEROUS_BYPASS_ARG]),
    )
    return RecoveryConfig(
        thread_id=option_getter(session, "@codex_thread_id", ""),
        primary_model=option_getter(
            session, "@codex_primary_model", "gpt-5.6-sol"
        ),
        primary_reasoning_effort=option_getter(
            session, "@codex_primary_effort", "max"
        ),
        compact_model=option_getter(
            session, "@codex_compact_model", "gpt-5.6-luna"
        ),
        compact_reasoning_effort=option_getter(
            session, "@codex_compact_effort", "xhigh"
        ),
        codex_args=tuple(json.loads(codex_args_json)),
        cooldown_seconds=int(
            option_getter(session, "@codex_cooldown_seconds", "300")
        ),
        max_recoveries=int(
            option_getter(session, "@codex_max_recoveries", "0")
        ),
        compact_wait_seconds=int(
            option_getter(session, "@codex_compact_wait_seconds", "600")
        ),
        thread_max_compactions=int(
            option_getter(session, "@codex_thread_max_compactions", "0")
        ),
        thread_max_rollout_bytes=int(
            option_getter(
                session,
                "@codex_thread_max_rollout_bytes",
                str(512 * 1024 * 1024),
            )
        ),
        thread_max_context_tokens=int(
            option_getter(session, "@codex_thread_max_context_tokens", "0")
        ),
        thread_no_progress_tokens=int(
            option_getter(session, "@codex_thread_no_progress_tokens", "1000000")
        ),
        thread_no_event_seconds=int(
            option_getter(session, "@codex_thread_no_event_seconds", "1800")
        ),
        thread_health_poll_seconds=int(
            option_getter(session, "@codex_thread_health_poll_seconds", "30")
        ),
        thread_max_repeated_content=int(
            option_getter(session, "@codex_thread_max_repeated_content", "3")
        ),
        thread_max_repeated_commands=int(
            option_getter(session, "@codex_thread_max_repeated_commands", "3")
        ),
        resume_prompt=option_getter(
            session, "@codex_resume_prompt", DEFAULT_RESUME_PROMPT
        ),
    )


def _append_log(log_path: Path, message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(f"[{timestamp}] [codex-goal-guardian] {message}\n")


def run_guardian(
    session: str,
    *,
    poll_seconds: float = 5,
    root_dir: Path | None = None,
) -> None:
    root = root_dir or Path(__file__).resolve().parents[1]
    log_path = default_log_path()
    last_status = ""
    active_screen_check_pending = True
    _append_log(log_path, f"guardian started: session={session}")

    while True:
        try:
            session_exists = tmux_session_exists(session)
            if session_exists:
                log_path = Path(
                    _tmux_option(session, "@codex_log_path", str(default_log_path()))
                ).expanduser()
            config = _recovery_config(session) if session_exists else None
            pending_incident: tuple[str, str] | None = None

            def stalled_screen() -> bool:
                nonlocal pending_incident
                assert config is not None
                pending_incident = _unhandled_recovery_incident_on_screen(
                    session,
                    thread_id=config.thread_id,
                )
                return pending_incident is not None

            def recover() -> None:
                assert config is not None
                if pending_incident is None:
                    return
                incident_id, reason = pending_incident
                if not _claim_tmux_recovery_incident_id(session, incident_id):
                    _append_log(
                        log_path,
                        "visible fatal incident already claimed by monitor: "
                        f"{incident_id}",
                    )
                    return
                goal_state = recovery_goal_state_on_screen(session)
                recovery_attempt = _next_recovery_attempt(session)
                _mark_verification_pending(session, config)
                if reason == "upstream_access_denied":
                    _set_pending_thread_rotation(session, recovery_attempt)
                _append_log(
                    log_path,
                    "visible recoverable error claimed during guardian handoff: "
                    f"{reason}; recovery #{recovery_attempt}",
                )
                execute_steps(
                    session,
                    build_recovery_steps(
                        config,
                        reason=reason,
                        recovery_attempt=recovery_attempt,
                        resume_goal=goal_state != "blocked",
                        resume_stalled_goal=goal_state == "stalled",
                        goal_objective=(
                            find_latest_goal_objective(thread_id=config.thread_id)
                            if reason == "upstream_access_denied"
                            else None
                        ),
                    ),
                )

            def restart_after_update() -> None:
                assert config is not None
                goal_state = recovery_goal_state_on_screen(session)
                _append_log(
                    log_path,
                    "Codex update completed; restarting pinned thread",
                )
                execute_steps(
                    session,
                    build_post_update_restart_steps(
                        config,
                        resume_goal=goal_state not in {"blocked", "stalled"},
                    ),
                )

            def attach_monitor() -> None:
                assert config is not None
                pipe_command = monitor_pipe_command(
                    root_dir=str(root),
                    session=session,
                    thread_id=config.thread_id,
                    primary_model=config.primary_model,
                    primary_reasoning_effort=config.primary_reasoning_effort,
                    compact_model=config.compact_model,
                    compact_reasoning_effort=config.compact_reasoning_effort,
                    codex_args=list(config.codex_args),
                    resume_prompt=config.resume_prompt,
                    log_path=str(log_path),
                    cooldown_seconds=config.cooldown_seconds,
                    max_recoveries=config.max_recoveries,
                    compact_wait_seconds=config.compact_wait_seconds,
                    thread_max_compactions=config.thread_max_compactions,
                    thread_max_rollout_bytes=config.thread_max_rollout_bytes,
                    thread_max_context_tokens=config.thread_max_context_tokens,
                    thread_no_progress_tokens=config.thread_no_progress_tokens,
                    thread_no_event_seconds=config.thread_no_event_seconds,
                    thread_health_poll_seconds=config.thread_health_poll_seconds,
                    thread_max_repeated_content=config.thread_max_repeated_content,
                    thread_max_repeated_commands=config.thread_max_repeated_commands,
                )
                subprocess.run(
                    ["tmux", "pipe-pane", "-o", "-t", session, pipe_command],
                    check=True,
                )

            status = guard_once(
                session_exists=lambda: tmux_session_exists(session),
                pipe_active=lambda: _pipe_active(session),
                stalled_screen=stalled_screen,
                recover=recover,
                attach_monitor=attach_monitor,
                update_restart_needed=lambda: _guardian_update_restart_needed(
                    session
                ),
                restart_after_update=restart_after_update,
                inspect_active_screen=active_screen_check_pending,
            )
            active_screen_check_pending = status in {
                "session_missing",
                "restarted_after_update",
            }
            if status != last_status or status not in {"healthy", "session_missing"}:
                _append_log(log_path, f"status={status}")
                last_status = status
        except Exception as exc:
            _append_log(log_path, f"iteration failed: {type(exc).__name__}: {exc}")
        time.sleep(poll_seconds)
