"""tmux command helpers for the Codex watchdog."""

from __future__ import annotations

import json
import fcntl
import hashlib
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from .bindings import save_binding_runtime_state
from .paths import state_dir
from .recovery import (
    RecoveryConfig,
    RecoveryStep,
    build_codex_update_completion_steps,
    build_codex_update_steps,
)
from .sessions import compaction_event_exists_after, find_thread_rollout_path


PAUSED_GOAL_PICKER_MARKERS = (
    "Resume paused goal?",
    "Resume goal",
    "Leave paused",
)
GOAL_STATE_MARKERS = (
    ("pursuing", "Pursuing goal"),
    ("pursuing", "Goal active Objective:"),
    ("blocked", "Goal blocked (/goal resume)"),
    ("stalled", "Goal stalled (/goal resume)"),
    ("paused", "Goal paused (/goal resume)"),
    ("usage_limited", "Goal hit usage limits (/goal resume)"),
    ("achieved", "Goal achieved"),
    ("achieved", "Goal complete"),
    ("achieved", "Goal completed"),
)
UPDATE_PROMPT_RE = re.compile(
    r"Update available!\s+v?(?P<current>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)"
    r"\s*->\s*v?(?P<latest>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)"
)
CODEX_VERSION_RE = re.compile(
    r"\b(?P<version>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b"
)
TEXT_SUBMIT_SETTLE_SECONDS = 0.5
TEXT_SUBMIT_RETRY_COUNT = 10
TEXT_SUBMIT_CAPTURE_LINES = 200
ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x1b\x07]*(?:\x07|\x1b\\)|[@-_])"
)
SHELL_COMMANDS = {"bash", "zsh", "sh", "fish"}
SHELL_PROMPT_RE = re.compile(
    r"^\s*(?:\([^)\r\n]+\)\s+)?"
    r"(?:(?:[\w.-]+@[\w.-]+:[^#$\r\n]*)|"
    r"(?:bash|zsh|sh|fish)(?:-[\w.-]+)?)[#$%]\s*"
    r"|^\s*(?:\([^)\r\n]+\)\s+)?[$#]\s+"
)
PENDING_UPDATE_OPTION = "@codex_pending_update_version"
PENDING_THREAD_ROTATION_OPTION = "@codex_pending_thread_rotation_count"
SUCCESSFUL_COMPACTIONS_OPTION = "@codex_successful_compactions"
LAST_RECOVERY_INCIDENT_OPTION = "@codex_last_recovery_incident_id"


def normalize_terminal_text(value: str) -> str:
    """Remove terminal control sequences and normalize visual line wrapping."""
    return " ".join(ANSI_ESCAPE_RE.sub("", value).split())


def tmux_recovery_count(target: str) -> int:
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


def save_tmux_recovery_count(target: str, count: int) -> None:
    normalized_count = max(0, count)
    subprocess.run(
        [
            "tmux",
            "set-option",
            "-t",
            target,
            "@codex_recovery_count",
            str(normalized_count),
        ],
        check=True,
    )
    save_binding_runtime_state(
        session=target,
        recovery_count=normalized_count,
        successful_compactions=tmux_successful_compactions(target),
    )


def tmux_successful_compactions(target: str) -> int:
    result = subprocess.run(
        ["tmux", "show-option", "-v", "-t", target, SUCCESSFUL_COMPACTIONS_OPTION],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return max(0, int(result.stdout.strip())) if result.returncode == 0 else 0
    except ValueError:
        return 0


def save_tmux_successful_compactions(target: str, count: int) -> None:
    normalized_count = max(0, count)
    subprocess.run(
        [
            "tmux",
            "set-option",
            "-t",
            target,
            SUCCESSFUL_COMPACTIONS_OPTION,
            str(normalized_count),
        ],
        check=True,
    )
    save_binding_runtime_state(
        session=target,
        recovery_count=tmux_recovery_count(target),
        successful_compactions=normalized_count,
    )


def pending_thread_rotation_count(target: str) -> int | None:
    result = subprocess.run(
        ["tmux", "show-option", "-v", "-t", target, PENDING_THREAD_ROTATION_OPTION],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        return max(0, int(result.stdout.strip()))
    except ValueError:
        return None


def set_pending_thread_rotation(target: str, count: int) -> None:
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


def clear_pending_thread_rotation(target: str) -> None:
    subprocess.run(
        [
            "tmux",
            "set-option",
            "-u",
            "-t",
            target,
            PENDING_THREAD_ROTATION_OPTION,
        ],
        check=True,
    )


def tmux_recovery_incident_id(target: str) -> str:
    result = subprocess.run(
        ["tmux", "show-option", "-v", "-t", target, LAST_RECOVERY_INCIDENT_OPTION],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def save_tmux_recovery_incident_id(target: str, incident_id: str) -> None:
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


def claim_tmux_recovery_incident_id(
    target: str,
    incident_id: str,
    *,
    option_getter=None,
    option_saver=None,
    lock_path: Path | None = None,
) -> bool:
    option_getter = option_getter or tmux_recovery_incident_id
    option_saver = option_saver or save_tmux_recovery_incident_id
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


def paused_goal_picker_visible(text: str) -> bool:
    return all(marker in text for marker in PAUSED_GOAL_PICKER_MARKERS)


def goal_state_from_text(text: str) -> str | None:
    latest_state: str | None = None
    latest_index = -1
    for state, marker in GOAL_STATE_MARKERS:
        index = text.rfind(marker)
        if index > latest_index:
            latest_index = index
            latest_state = state
    return latest_state


def update_prompt_version(text: str) -> str | None:
    match = UPDATE_PROMPT_RE.search(text)
    if (
        match is None
        or "Update now (runs" not in text
        or "Skip until next version" not in text
    ):
        return None
    return match.group("latest")


def capture_update_prompt_version(
    target: str, *, runner=subprocess.run
) -> str | None:
    result = runner(
        ["tmux", "capture-pane", "-p", "-t", target],
        capture_output=True,
        text=True,
        check=False,
    )
    if getattr(result, "returncode", 0) != 0:
        return None
    visible_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not visible_lines or "Update available!" not in visible_lines[0]:
        return None
    return update_prompt_version(result.stdout)


def _version_key(value: str) -> tuple[int, int, int, int]:
    core, separator, _suffix = value.partition("-")
    numbers = tuple(int(part) for part in core.split("+")[0].split("."))
    if len(numbers) != 3:
        raise ValueError(f"unsupported Codex version: {value}")
    return (*numbers, 0 if separator else 1)


def _installed_codex_version(*, runner=subprocess.run) -> str | None:
    try:
        result = runner(
            ["codex", "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    match = CODEX_VERSION_RE.search(result.stdout)
    return match.group("version") if match else None


def _wait_for_installed_codex_version(
    *,
    runner=subprocess.run,
    sleeper=time.sleep,
    attempts: int = 10,
) -> str | None:
    for attempt in range(max(1, attempts)):
        actual_version = _installed_codex_version(runner=runner)
        if actual_version is not None:
            return actual_version
        if attempt + 1 < attempts:
            sleeper(0.5)
    return None


def ensure_codex_version(
    expected_version: str,
    *,
    runner=subprocess.run,
    sleeper=time.sleep,
) -> str:
    actual_version = _wait_for_installed_codex_version(
        runner=runner,
        sleeper=sleeper,
    )
    if actual_version is not None and _version_key(actual_version) >= _version_key(
        expected_version
    ):
        return actual_version

    runner(["codex", "update"], check=True)
    actual_version = _wait_for_installed_codex_version(
        runner=runner,
        sleeper=sleeper,
    )
    if actual_version is None or _version_key(actual_version) < _version_key(
        expected_version
    ):
        raise RuntimeError(
            "Codex update did not install the requested version: "
            f"expected at least {expected_version}, got {actual_version or '<unknown>'}"
        )
    return actual_version


def commands_for_step(target: str, step: RecoveryStep) -> list[list[str]]:
    if step.kind == "key":
        return [["tmux", "send-keys", "-t", target, step.value]]
    if step.kind == "text":
        return [
            ["tmux", "send-keys", "-t", target, "-l", step.value],
            ["tmux", "send-keys", "-t", target, "Enter"],
        ]
    if step.kind == "sleep":
        return []
    raise ValueError(f"unsupported recovery step kind: {step.kind}")


def _submit_text(
    target: str,
    value: str,
    *,
    runner=subprocess.run,
    sleeper=time.sleep,
) -> None:
    commands = commands_for_step(target, RecoveryStep("text", value))
    runner(commands[0], check=True)
    # Codex can still be mounting its composer after the text arrives. Only
    # retry when the exact submitted value is still in the current input block
    # (or a known selection prompt is still visible).
    for attempt in range(TEXT_SUBMIT_RETRY_COUNT + 1):
        sleeper(TEXT_SUBMIT_SETTLE_SECONDS)
        enter_result = runner(commands[1], check=True)
        # Lightweight test/dry-run runners may not return a process result.
        if not hasattr(enter_result, "returncode"):
            return
        if attempt == TEXT_SUBMIT_RETRY_COUNT:
            return
        try:
            result = runner(
                [
                    "tmux",
                    "capture-pane",
                    "-p",
                    "-J",
                    "-t",
                    target,
                    "-S",
                    f"-{TEXT_SUBMIT_CAPTURE_LINES}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return
        if getattr(result, "returncode", 0) != 0:
            return
        screen = getattr(result, "stdout", "")
        pending_kind = _submission_pending_kind(screen, value)
        if pending_kind is None:
            return
        if pending_kind == "shell":
            # A shell command may remain in scrollback while Codex is already
            # running. Retry only when the pane is still an interactive shell.
            pane_command = _current_pane_command(target, runner=runner)
            if pane_command not in SHELL_COMMANDS:
                return


def _submission_is_pending(screen: str, normalized_value: str) -> bool:
    return _submission_pending_kind(screen, normalized_value) is not None


def _submission_pending_kind(screen: str, value: str) -> str | None:
    lines = ANSI_ESCAPE_RE.sub("", screen).splitlines()
    normalized_value = normalize_terminal_text(value)

    prompt_indices: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        if line.lstrip().startswith("›"):
            prompt_indices.append((index, "codex"))
        elif SHELL_PROMPT_RE.match(line):
            prompt_indices.append((index, "shell"))
    if prompt_indices:
        prompt_index, prompt_kind = prompt_indices[-1]
        composer = normalize_terminal_text(" ".join(lines[prompt_index:]))
        if "Create a plan?" in composer:
            return "picker"
        if not normalized_value:
            return None
        if _submitted_text_matches(composer, normalized_value):
            return prompt_kind
        return None

    # A picker can be rendered without a composer marker during startup.
    tail = normalize_terminal_text(
        " ".join(lines[-TEXT_SUBMIT_CAPTURE_LINES:])
    )
    return "picker" if "Create a plan?" in tail else None


def _submitted_text_matches(composer: str, expected: str) -> bool:
    if expected in composer:
        return True
    if len(expected) < 48:
        return False
    # Very long input can scroll its prefix out of the capture window. Seeing
    # both stable ends in the current composer still identifies the pending
    # submission without matching an older transcript line.
    return expected[:48] in composer and expected[-48:] in composer


def _current_pane_command(target: str, *, runner=subprocess.run) -> str:
    try:
        result = runner(
            [
                "tmux",
                "display-message",
                "-p",
                "-t",
                target,
                "#{pane_current_command}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if getattr(result, "returncode", 0) != 0:
        return ""
    return getattr(result, "stdout", "").strip()


def wait_for_pane_state(
    target: str,
    *,
    state: str,
    timeout_seconds: float,
    runner=subprocess.run,
    sleeper=time.sleep,
    now=time.monotonic,
) -> None:
    if state not in {"shell", "codex"}:
        raise ValueError(f"unsupported pane state: {state}")

    deadline = now() + timeout_seconds
    last_command = ""
    while True:
        pane_result = runner(
            [
                "tmux",
                "display-message",
                "-p",
                "-t",
                target,
                "#{pane_pid}\t#{pane_current_command}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        pane_pid_text, _, last_command = pane_result.stdout.strip().partition("\t")
        pane_pid = int(pane_pid_text)

        process_result = runner(
            ["ps", "-eo", "pid=,ppid=,comm=,args="],
            capture_output=True,
            text=True,
            check=True,
        )
        processes: dict[int, tuple[int, str, str]] = {}
        children: dict[int, list[int]] = {}
        for line in process_result.stdout.splitlines():
            parts = line.strip().split(None, 3)
            if len(parts) < 3:
                continue
            pid_text, parent_text, command = parts[:3]
            arguments = parts[3] if len(parts) == 4 else command
            try:
                pid = int(pid_text)
                parent = int(parent_text)
            except ValueError:
                continue
            processes[pid] = (parent, command, arguments)
            children.setdefault(parent, []).append(pid)

        descendants = []
        pending = list(children.get(pane_pid, []))
        while pending:
            pid = pending.pop()
            descendants.append(pid)
            pending.extend(children.get(pid, []))

        codex_running = last_command in {"node", "codex"} or any(
            command in {"node", "codex"} and "codex" in arguments
            for pid in descendants
            if (process := processes.get(pid)) is not None
            for _, command, arguments in [process]
        )
        shell_ready = last_command in {"bash", "zsh", "sh", "fish"} and not codex_running
        if (state == "codex" and codex_running) or (state == "shell" and shell_ready):
            return
        if now() >= deadline:
            raise TimeoutError(
                f"tmux pane {target} did not reach {state}; "
                f"last command was {last_command or '<empty>'}"
            )
        sleeper(0.25)


def pane_codex_running(target: str, *, runner=subprocess.run) -> bool:
    try:
        wait_for_pane_state(
            target,
            state="codex",
            timeout_seconds=0,
            runner=runner,
        )
    except (OSError, RuntimeError, TimeoutError, ValueError):
        return False
    return True


def handle_goal_prompt(
    target: str,
    *,
    action: str,
    prompt: str,
    timeout_seconds: float = 600,
    poll_seconds: float = 0.5,
    send_fallback_prompt: bool = True,
    runner=subprocess.run,
    sleeper=time.sleep,
    now=time.monotonic,
) -> bool:
    if action not in {"leave_paused", "resume", "resume_stalled"}:
        raise ValueError(f"unsupported goal prompt action: {action}")

    deadline = now() + max(0, timeout_seconds)
    while True:
        result = runner(
            ["tmux", "capture-pane", "-p", "-t", target],
            capture_output=True,
            text=True,
            check=True,
        )
        goal_state = goal_state_from_text(result.stdout)
        if goal_state == "blocked":
            print(
                "[codex-goal-watchdog] goal blocked; "
                "waiting for manual /goal resume",
                flush=True,
            )
            return True
        if goal_state == "stalled":
            if action == "resume_stalled":
                _submit_text(
                    target,
                    "/goal resume",
                    runner=runner,
                    sleeper=sleeper,
                )
            return True
        picker_visible = paused_goal_picker_visible(result.stdout)
        if picker_visible:
            keys = ["Down", "Enter"] if action == "leave_paused" else ["Enter"]
            for key in keys:
                runner(["tmux", "send-keys", "-t", target, key], check=True)
            return True

        goal_resume_required = goal_state in {"paused", "usage_limited"}
        if goal_resume_required:
            if action in {"resume", "resume_stalled"}:
                _submit_text(
                    target,
                    "/goal resume",
                    runner=runner,
                    sleeper=sleeper,
                )
            return True

        if action in {"resume", "resume_stalled"} and goal_state == "pursuing":
            return True
        if now() >= deadline:
            break
        sleeper(poll_seconds)

    if action in {"resume", "resume_stalled"} and send_fallback_prompt:
        _submit_text(target, prompt, runner=runner, sleeper=sleeper)
    return False


def execute_steps(
    target: str,
    steps: list[RecoveryStep],
    *,
    dry_run: bool = False,
    runner=subprocess.run,
    sleeper=time.sleep,
) -> None:
    compaction_offsets: dict[str, tuple[Path, int]] = {}
    for step in steps:
        if step.kind == "sleep":
            if not dry_run:
                sleeper(float(step.value))
            continue
        if step.kind in {"wait_shell", "wait_codex"}:
            state = "shell" if step.kind == "wait_shell" else "codex"
            if dry_run:
                print(
                    f"DRY-RUN: wait for {state} (timeout {step.value}s)",
                    flush=True,
                )
                continue
            wait_for_pane_state(
                target,
                state=state,
                timeout_seconds=float(step.value),
                runner=runner,
                sleeper=sleeper,
            )
            continue
        if step.kind == "ensure_codex_version":
            if dry_run:
                print(
                    f"DRY-RUN: ensure Codex version >= {step.value}",
                    flush=True,
                )
                continue
            ensure_codex_version(step.value, runner=runner)
            continue
        if step.kind == "update_codex":
            if dry_run:
                print("DRY-RUN: codex update", flush=True)
                continue
            runner(["codex", "update"], check=True)
            continue
        if step.kind == "mark_compaction":
            path = find_thread_rollout_path(thread_id=step.value)
            if path is None:
                raise RuntimeError(f"rollout not found for thread {step.value}")
            compaction_offsets[step.value] = (path, path.stat().st_size)
            continue
        if step.kind == "wait_compaction":
            if dry_run:
                print(
                    "DRY-RUN: wait for context_compacted "
                    f"(timeout {step.timeout_seconds}s)",
                    flush=True,
                )
                continue
            path, offset = compaction_offsets[step.value]
            timeout = step.timeout_seconds or 600
            deadline = time.monotonic() + timeout
            while not compaction_event_exists_after(path, offset=offset):
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"thread {step.value} did not emit context_compacted "
                        f"within {timeout}s"
                    )
                sleeper(1)
            continue
        if step.kind in {
            "leave_goal_paused",
            "resume_goal_or_prompt",
            "resume_stalled_goal_or_prompt",
        }:
            action = {
                "leave_goal_paused": "leave_paused",
                "resume_goal_or_prompt": "resume",
                "resume_stalled_goal_or_prompt": "resume_stalled",
            }[step.kind]
            if dry_run:
                print(f"DRY-RUN: handle goal prompt ({action})", flush=True)
                continue
            handle_goal_prompt(
                target,
                action=action,
                prompt=step.value,
                runner=runner,
                sleeper=sleeper,
            )
            continue
        if step.kind == "text" and not dry_run:
            _submit_text(
                target,
                step.value,
                runner=runner,
                sleeper=sleeper,
            )
            continue
        for command in commands_for_step(target, step):
            if dry_run:
                print("DRY-RUN:", shlex.join(command), flush=True)
                continue
            runner(command, check=True)


def pending_update_version(target: str) -> str:
    result = subprocess.run(
        ["tmux", "show-option", "-v", "-t", target, PENDING_UPDATE_OPTION],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def set_pending_update_version(target: str, version: str) -> None:
    subprocess.run(
        ["tmux", "set-option", "-t", target, PENDING_UPDATE_OPTION, version],
        check=True,
    )


def clear_pending_update_version(target: str) -> None:
    subprocess.run(
        ["tmux", "set-option", "-u", "-t", target, PENDING_UPDATE_OPTION],
        check=True,
    )


def run_codex_update(
    target: str,
    config: RecoveryConfig,
    expected_version: str,
    *,
    resume_goal: bool = True,
) -> None:
    set_pending_update_version(target, expected_version)
    execute_steps(
        target,
        build_codex_update_steps(
            config,
            expected_version,
            resume_goal=resume_goal,
        ),
    )
    clear_pending_update_version(target)


def resume_interrupted_update(target: str, config: RecoveryConfig) -> None:
    current_goal_state = recovery_goal_state_on_screen(target)
    visible_version = capture_update_prompt_version(target)
    if visible_version is not None:
        print(
            "[codex-goal-watchdog] installing visible Codex update: "
            f"target={visible_version}",
            flush=True,
        )
        run_codex_update(
            target,
            config,
            visible_version,
            resume_goal=current_goal_state not in {"blocked", "stalled"},
        )
        return

    pending_version = pending_update_version(target)
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
    clear_pending_update_version(target)


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


def monitor_pipe_command(
    *,
    root_dir: str,
    python_executable: str = sys.executable,
    session: str,
    thread_id: str,
    primary_model: str,
    primary_reasoning_effort: str,
    compact_model: str,
    compact_reasoning_effort: str,
    codex_args: list[str],
    resume_prompt: str,
    log_path: str,
    cooldown_seconds: int = 300,
    max_recoveries: int = 0,
    compact_wait_seconds: int = 600,
    thread_max_compactions: int = 3,
    thread_max_rollout_bytes: int = 512 * 1024 * 1024,
    thread_max_context_tokens: int = 0,
    thread_no_progress_tokens: int = 1_000_000,
    thread_no_event_seconds: int = 30 * 60,
    thread_health_poll_seconds: int = 30,
    thread_max_repeated_content: int = 3,
    thread_max_repeated_commands: int = 3,
) -> str:
    root = str(Path(root_dir).resolve())
    parts = [
        f"PYTHONPATH={shlex.quote(root)}",
        python_executable,
        "-m",
        "codex_goal_watchdog",
        "monitor",
        "--session",
        session,
        "--thread-id",
        thread_id,
        "--primary-model",
        primary_model,
        "--primary-reasoning-effort",
        primary_reasoning_effort,
        "--compact-model",
        compact_model,
        "--compact-reasoning-effort",
        compact_reasoning_effort,
        "--codex-args-json",
        json.dumps(codex_args),
        "--resume-prompt",
        resume_prompt,
        "--cooldown-seconds",
        str(cooldown_seconds),
        "--max-recoveries",
        str(max_recoveries),
        "--compact-wait-seconds",
        str(compact_wait_seconds),
        "--thread-max-compactions",
        str(thread_max_compactions),
        "--thread-max-rollout-bytes",
        str(thread_max_rollout_bytes),
        "--thread-max-context-tokens",
        str(thread_max_context_tokens),
        "--thread-no-progress-tokens",
        str(thread_no_progress_tokens),
        "--thread-no-event-seconds",
        str(thread_no_event_seconds),
        "--thread-health-poll-seconds",
        str(thread_health_poll_seconds),
        "--thread-max-repeated-content",
        str(thread_max_repeated_content),
        "--thread-max-repeated-commands",
        str(thread_max_repeated_commands),
    ]
    command = shlex.join(parts)
    return f"{command} >> {shlex.quote(log_path)} 2>&1"
