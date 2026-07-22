"""tmux command helpers for the Codex watchdog."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from .recovery import RecoveryStep
from .sessions import compaction_event_exists_after, find_thread_rollout_path


PAUSED_GOAL_PICKER_MARKERS = (
    "Resume paused goal?",
    "Resume goal",
    "Leave paused",
)
UPDATE_PROMPT_RE = re.compile(
    r"Update available!\s+v?(?P<current>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)"
    r"\s*->\s*v?(?P<latest>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)"
)
CODEX_VERSION_RE = re.compile(
    r"\b(?P<version>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b"
)


def paused_goal_picker_visible(text: str) -> bool:
    return all(marker in text for marker in PAUSED_GOAL_PICKER_MARKERS)


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
    if action not in {"leave_paused", "resume"}:
        raise ValueError(f"unsupported goal prompt action: {action}")

    deadline = now() + max(0, timeout_seconds)
    while True:
        result = runner(
            ["tmux", "capture-pane", "-p", "-t", target],
            capture_output=True,
            text=True,
            check=True,
        )
        picker_visible = paused_goal_picker_visible(result.stdout)
        if picker_visible:
            keys = ["Down", "Enter"] if action == "leave_paused" else ["Enter"]
            for key in keys:
                runner(["tmux", "send-keys", "-t", target, key], check=True)
            return True

        goal_resume_required = any(
            status in result.stdout
            for status in (
                "Goal paused (/goal resume)",
                "Goal blocked (/goal resume)",
                "Goal hit usage limits (/goal resume)",
            )
        )
        if goal_resume_required:
            if action == "resume":
                for command in commands_for_step(
                    target, RecoveryStep("text", "/goal resume")
                ):
                    runner(command, check=True)
            return True

        if action == "resume" and "Pursuing goal" in result.stdout:
            return True
        if now() >= deadline:
            break
        sleeper(poll_seconds)

    if action == "resume" and send_fallback_prompt:
        for command in commands_for_step(target, RecoveryStep("text", prompt)):
            runner(command, check=True)
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
        if step.kind in {"leave_goal_paused", "resume_goal_or_prompt"}:
            action = "leave_paused" if step.kind == "leave_goal_paused" else "resume"
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
        for command in commands_for_step(target, step):
            if dry_run:
                print("DRY-RUN:", shlex.join(command), flush=True)
                continue
            runner(command, check=True)


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
    compact_wait_seconds: int = 120,
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
    ]
    command = shlex.join(parts)
    return f"{command} >> {shlex.quote(log_path)} 2>&1"
