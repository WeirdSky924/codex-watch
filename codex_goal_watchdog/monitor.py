"""Monitor tmux pipe output and trigger Codex recovery."""

from __future__ import annotations

import codecs
import fcntl
import hashlib
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO

from .bindings import save_session_binding
from .paths import state_dir
from .recovery import (
    RecoveryConfig,
    build_codex_update_completion_steps,
    build_codex_update_steps,
    build_recovery_steps,
    classify_recovery_message,
    classify_recovery_reason,
)
from .sessions import find_active_cli_thread_id, find_latest_task_failure
from .tmux_control import (
    capture_update_prompt_version,
    execute_steps,
    goal_state_from_text,
    handle_goal_prompt,
    paused_goal_picker_visible,
    update_prompt_version,
)


ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x1b\x07]*(?:\x07|\x1b\\)|[@-_])"
)
ROLLING_BUFFER_SIZE = 8192
GOAL_RESUME_STATUS_MARKERS = (
    "Goal paused (/goal resume)",
    "Goal hit usage limits (/goal resume)",
)
GOAL_RESUME_RETRY_SECONDS = 10
PENDING_UPDATE_OPTION = "@codex_pending_update_version"
LAST_RECOVERY_INCIDENT_OPTION = "@codex_last_recovery_incident_id"
RECOVERABLE_GOAL_STATES = {"pursuing", "blocked", "stalled"}


def normalize_terminal_text(value: str) -> str:
    """Remove terminal control sequences and normalize visual line wrapping."""
    return " ".join(ANSI_ESCAPE_RE.sub("", value).split())


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
    stream: BinaryIO, *, chunk_size: int = 4096
) -> Iterable[str]:
    """Yield available terminal bytes without waiting for a newline."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    read_chunk = getattr(stream, "read1", stream.read)
    while chunk := read_chunk(chunk_size):
        decoded = decoder.decode(chunk)
        if decoded:
            yield decoded
    tail = decoder.decode(b"", final=True)
    if tail:
        yield tail


def run_monitor(
    *,
    lines: Iterable[str],
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
    latest_goal_state: str | None = None
    last_recovery_incident_id = initial_recovery_incident_id
    for line in lines:
        resolved_thread_id = (
            resolve_thread_id(target) if resolve_thread_id is not None else None
        )
        if resolved_thread_id and resolved_thread_id != config.thread_id:
            config = replace(config, thread_id=resolved_thread_id)
            controller = RecoveryController(config, initial_recovery_count=0)
            rolling_output = ""
            last_goal_resume_at = None
            latest_goal_state = None
            last_recovery_incident_id = ""
            if save_thread_id is not None:
                save_thread_id(resolved_thread_id)
            emit(
                "[codex-goal-watchdog] rebound thread after /clear: "
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
        rolling_output = normalize_terminal_text(f"{rolling_output} {normalized_line}")
        rolling_output = rolling_output[-ROLLING_BUFFER_SIZE:]
        observed_at = now()
        recovery_reason = classify_recovery_reason(rolling_output)
        if recovery_reason is not None and resolve_recovery_incident is not None:
            incident = resolve_recovery_incident(config.thread_id)
            if incident is None or incident[1] != recovery_reason:
                emit(
                    "[codex-goal-watchdog] ignored terminal error without "
                    f"matching rollout event: {recovery_reason}"
                )
                rolling_output = ""
                continue
            incident_id, _ = incident
            if incident_id == last_recovery_incident_id:
                emit(
                    "[codex-goal-watchdog] ignored redrawn fatal event: "
                    f"{incident_id}"
                )
                rolling_output = ""
                continue
            last_recovery_incident_id = incident_id
            if (
                claim_recovery_incident_id is not None
                and not claim_recovery_incident_id(incident_id)
            ):
                emit(
                    "[codex-goal-watchdog] ignored fatal incident claimed by "
                    f"another recovery owner: {incident_id}"
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
            emit(
                f"[codex-goal-watchdog] recovery #{controller.recovery_count}: "
                f"{event.reason}"
            )
            if save_recovery_count is not None:
                save_recovery_count(controller.recovery_count)
            run_execute(
                target,
                build_recovery_steps(
                    config,
                    reason=event.reason,
                    recovery_attempt=controller.recovery_count,
                    resume_goal=effective_goal_state != "blocked",
                    resume_stalled_goal=effective_goal_state == "stalled",
                ),
            )
            continue

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
            emit("[codex-goal-watchdog] resuming paused goal")
            run_resume_goal(target)
            last_goal_resume_at = observed_at
            rolling_output = ""


def _tmux_recovery_count(target: str) -> int:
    result = subprocess.run(
        ["tmux", "show-option", "-v", "-t", target, "@codex_recovery_count"],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return max(0, int(result.stdout.strip())) if result.returncode == 0 else 0
    except ValueError:
        return 0


def _save_tmux_recovery_count(target: str, count: int) -> None:
    subprocess.run(
        [
            "tmux",
            "set-option",
            "-t",
            target,
            "@codex_recovery_count",
            str(max(0, count)),
        ],
        check=True,
    )


def _tmux_recovery_incident_id(target: str) -> str:
    result = subprocess.run(
        ["tmux", "show-option", "-v", "-t", target, LAST_RECOVERY_INCIDENT_OPTION],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _save_tmux_recovery_incident_id(target: str, incident_id: str) -> None:
    subprocess.run(
        [
            "tmux",
            "set-option",
            "-t",
            target,
            LAST_RECOVERY_INCIDENT_OPTION,
            incident_id,
        ],
        check=True,
    )


def _claim_tmux_recovery_incident_id(
    target: str,
    incident_id: str,
    *,
    option_getter: Callable[[str], str] = _tmux_recovery_incident_id,
    option_saver: Callable[[str, str], None] = _save_tmux_recovery_incident_id,
    lock_path: Path | None = None,
) -> bool:
    if lock_path is None:
        lock_root = state_dir()
        lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_root.chmod(0o700)
        session_key = hashlib.sha256(target.encode("utf-8")).hexdigest()[:16]
        lock_path = lock_root / f"recovery-incident-{session_key}.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_stream:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
        if option_getter(target) == incident_id:
            return False
        option_saver(target, incident_id)
        return True


def _save_tmux_thread_id(target: str, thread_id: str) -> None:
    subprocess.run(
        ["tmux", "set-option", "-t", target, "@codex_thread_id", thread_id],
        check=True,
    )
    _save_tmux_recovery_count(target, 0)
    pane_identity = _tmux_pane_identity(target)
    if pane_identity is not None:
        _, cwd = pane_identity
        save_session_binding(
            session=target,
            thread_id=thread_id,
            cwd=cwd,
        )


def _tmux_pane_identity(target: str) -> tuple[int, Path] | None:
    result = subprocess.run(
        [
            "tmux",
            "display-message",
            "-p",
            "-t",
            target,
            "#{pane_pid}\t#{pane_current_path}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    pid_text, separator, cwd_text = result.stdout.strip().partition("\t")
    if not separator or not cwd_text:
        return None
    try:
        return int(pid_text), Path(cwd_text).resolve()
    except (OSError, ValueError):
        return None


def _pending_update_version(target: str) -> str:
    result = subprocess.run(
        ["tmux", "show-option", "-v", "-t", target, PENDING_UPDATE_OPTION],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _set_pending_update_version(target: str, version: str) -> None:
    subprocess.run(
        ["tmux", "set-option", "-t", target, PENDING_UPDATE_OPTION, version],
        check=True,
    )


def _clear_pending_update_version(target: str) -> None:
    subprocess.run(
        ["tmux", "set-option", "-u", "-t", target, PENDING_UPDATE_OPTION],
        check=True,
    )


def _run_codex_update(
    target: str,
    config: RecoveryConfig,
    expected_version: str,
    *,
    resume_goal: bool = True,
) -> None:
    _set_pending_update_version(target, expected_version)
    execute_steps(
        target,
        build_codex_update_steps(
            config,
            expected_version,
            resume_goal=resume_goal,
        ),
    )
    _clear_pending_update_version(target)


def _resume_interrupted_update(target: str, config: RecoveryConfig) -> None:
    current_goal_state = recovery_goal_state_on_screen(target)
    visible_version = capture_update_prompt_version(target)
    if visible_version is not None:
        print(
            "[codex-goal-watchdog] installing visible Codex update: "
            f"target={visible_version}",
            flush=True,
        )
        _run_codex_update(
            target,
            config,
            visible_version,
            resume_goal=current_goal_state not in {"blocked", "stalled"},
        )
        return

    pending_version = _pending_update_version(target)
    if not pending_version:
        return
    print(
        "[codex-goal-watchdog] completing interrupted Codex update: "
        f"target={pending_version}",
        flush=True,
    )
    execute_steps(
        target,
        build_codex_update_completion_steps(
            config,
            pending_version,
            resume_goal=current_goal_state not in {"blocked", "stalled"},
        ),
    )
    _clear_pending_update_version(target)


def recovery_goal_state_on_screen(
    target: str, *, runner=subprocess.run
) -> str | None:
    result = runner(
        ["tmux", "capture-pane", "-p", "-t", target],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return goal_state_from_text(normalize_terminal_text(result.stdout))


def monitor_stdin(target: str, config: RecoveryConfig) -> None:
    print(f"[codex-goal-watchdog] monitor started: target={target}", flush=True)
    pane_identity = _tmux_pane_identity(target)

    def resolve_thread_id(_target: str) -> str | None:
        if pane_identity is None:
            return None
        pane_pid, cwd = pane_identity
        return find_active_cli_thread_id(pane_pid=pane_pid, cwd=cwd)

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
        current_incident = recovery_incident_for_thread(config.thread_id)
        if current_incident is not None:
            initial_incident_id = current_incident[0]
            _claim_tmux_recovery_incident_id(target, initial_incident_id)
    run_monitor(
        lines=iter_decoded_chunks(sys.stdin.buffer),
        target=target,
        config=config,
        initial_recovery_count=_tmux_recovery_count(target),
        save_recovery_count=lambda count: _save_tmux_recovery_count(target, count),
        resolve_thread_id=resolve_thread_id,
        save_thread_id=lambda thread_id: _save_tmux_thread_id(target, thread_id),
        resolve_recovery_incident=recovery_incident_for_thread,
        initial_recovery_incident_id=initial_incident_id,
        claim_recovery_incident_id=lambda incident_id: (
            _claim_tmux_recovery_incident_id(target, incident_id)
        ),
    )
