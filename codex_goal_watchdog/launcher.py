"""Launch helpers for running Codex under tmux."""

from __future__ import annotations

import shlex
import subprocess


DANGEROUS_BYPASS_ARG = "--dangerously-bypass-approvals-and-sandbox"


def normalize_codex_args(codex_args: list[str], *, safe_mode: bool) -> list[str]:
    normalized = list(codex_args)
    if not safe_mode and DANGEROUS_BYPASS_ARG not in normalized:
        normalized.insert(0, DANGEROUS_BYPASS_ARG)
    return normalized


def build_codex_command(
    *,
    model: str,
    reasoning_effort: str,
    codex_args: list[str] | tuple[str, ...] | None = None,
    resume_last: bool = False,
    resume_thread_id: str | None = None,
) -> list[str]:
    command = [
        "env",
        "-u",
        "NO_COLOR",
        "COLORTERM=truecolor",
        "codex",
        "--no-alt-screen",
        "-m",
        model,
        "-c",
        f'model_reasoning_effort="{reasoning_effort}"',
    ]
    if codex_args:
        command.extend(codex_args)
    if resume_thread_id:
        command.extend(["resume", resume_thread_id])
    elif resume_last:
        command.extend(["resume", "--last"])
    return command


def tmux_new_session_command(session: str, codex_command: list[str]) -> list[str]:
    return [
        "tmux",
        "new-session",
        "-d",
        "-s",
        session,
        f"{shlex.join(codex_command)}; exec bash",
    ]


def tmux_pipe_pane_command(session: str, pipe_command: str) -> list[str]:
    return ["tmux", "pipe-pane", "-o", "-t", session, pipe_command]


def tmux_attach_command(session: str) -> list[str]:
    return ["tmux", "attach", "-t", session]


def tmux_set_thread_id_command(session: str, thread_id: str) -> list[str]:
    return tmux_set_option_command(session, "@codex_thread_id", thread_id)


def tmux_set_option_command(session: str, name: str, value: str) -> list[str]:
    return ["tmux", "set-option", "-t", session, name, value]


def tmux_get_thread_id(session: str, *, runner=subprocess.run) -> str | None:
    result = runner(
        ["tmux", "show-option", "-v", "-t", session, "@codex_thread_id"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def tmux_session_exists(session: str, *, runner=subprocess.run) -> bool:
    result = runner(
        ["tmux", "has-session", "-t", session],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0
