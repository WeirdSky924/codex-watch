"""Independent supervisor for the tmux output monitor."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from .launcher import DANGEROUS_BYPASS_ARG, tmux_session_exists
from .monitor import normalize_terminal_text
from .paths import default_log_path
from .recovery import (
    DEFAULT_RESUME_PROMPT,
    RecoveryConfig,
    build_post_update_restart_steps,
    build_recovery_steps,
    classify_recovery_reason,
)
from .tmux_control import execute_steps, monitor_pipe_command


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
) -> str:
    if not session_exists():
        return "session_missing"
    if update_restart_needed is not None and update_restart_needed():
        if restart_after_update is None:
            raise ValueError("update restart callback is required")
        restart_after_update()
        return "restarted_after_update"
    if pipe_active():
        return "healthy"
    if stalled_screen():
        recover()
        attach_monitor()
        return "recovered_and_reattached"
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
    return attempt


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


def _recovery_reason_on_screen(session: str) -> str | None:
    result = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", session],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return classify_recovery_reason(normalize_terminal_text(result.stdout))


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
    _append_log(log_path, f"guardian started: session={session}")

    while True:
        try:
            session_exists = tmux_session_exists(session)
            if session_exists:
                log_path = Path(
                    _tmux_option(session, "@codex_log_path", str(default_log_path()))
                ).expanduser()
            config = _recovery_config(session) if session_exists else None

            def recover() -> None:
                assert config is not None
                reason = _recovery_reason_on_screen(session)
                if reason is None:
                    return
                recovery_attempt = _next_recovery_attempt(session)
                _append_log(
                    log_path,
                    "visible recoverable error found while monitor was down: "
                    f"{reason}; recovery #{recovery_attempt}",
                )
                execute_steps(
                    session,
                    build_recovery_steps(
                        config,
                        reason=reason,
                        recovery_attempt=recovery_attempt,
                    ),
                )

            def restart_after_update() -> None:
                assert config is not None
                _append_log(
                    log_path,
                    "Codex update completed; restarting pinned thread",
                )
                execute_steps(
                    session,
                    build_post_update_restart_steps(config),
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
                )
                subprocess.run(
                    ["tmux", "pipe-pane", "-o", "-t", session, pipe_command],
                    check=True,
                )

            status = guard_once(
                session_exists=lambda: tmux_session_exists(session),
                pipe_active=lambda: _pipe_active(session),
                stalled_screen=lambda: _recovery_reason_on_screen(session) is not None,
                recover=recover,
                attach_monitor=attach_monitor,
                update_restart_needed=lambda: _update_completed_on_shell(session),
                restart_after_update=restart_after_update,
            )
            if status != last_status or status not in {"healthy", "session_missing"}:
                _append_log(log_path, f"status={status}")
                last_status = status
        except Exception as exc:
            _append_log(log_path, f"iteration failed: {type(exc).__name__}: {exc}")
        time.sleep(poll_seconds)
