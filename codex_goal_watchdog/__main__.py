"""Command line entrypoint for codex-goal-watchdog."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .launcher import (
    build_codex_command,
    normalize_codex_args,
    tmux_attach_command,
    tmux_get_thread_id,
    tmux_new_session_command,
    tmux_pipe_pane_command,
    tmux_set_option_command,
    tmux_set_thread_id_command,
    tmux_session_exists,
)
from .monitor import monitor_stdin
from .guardian import run_guardian
from .paths import default_log_path
from .recovery import DEFAULT_RESUME_PROMPT, RecoveryConfig
from .sessions import find_latest_thread_id, validate_thread_id, wait_for_new_thread_id
from .tmux_control import monitor_pipe_command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codex-goal-watchdog",
        description="Run Codex CLI in tmux and auto-recover selected upstream stalls.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="start or attach to a watched tmux session")
    start.add_argument(
        "--version", action="version", version=f"codex-watch {__version__}"
    )
    start.add_argument("--session", default="codex-goal")
    start.add_argument("--primary-model", default="gpt-5.6-sol")
    start.add_argument("--primary-reasoning-effort", default="max")
    start.add_argument("--compact-model", default="gpt-5.6-luna")
    start.add_argument("--compact-reasoning-effort", default="xhigh")
    start.add_argument("--resume-prompt", default=DEFAULT_RESUME_PROMPT)
    start.add_argument("--cooldown-seconds", type=int, default=300)
    start.add_argument("--max-recoveries", type=int, default=0)
    start.add_argument("--compact-wait-seconds", type=int, default=600)
    start.add_argument("--log-path", default="")
    start.add_argument("--resume", action="store_true")
    start.add_argument("--thread-id", default="")
    start.add_argument(
        "--safe",
        action="store_true",
        help="do not add --dangerously-bypass-approvals-and-sandbox",
    )
    start.add_argument("--no-attach", action="store_true")
    start.add_argument("--dry-run", action="store_true")
    start.add_argument("codex_args", nargs=argparse.REMAINDER)

    monitor = subparsers.add_parser("monitor", help="internal tmux pipe monitor")
    monitor.add_argument("--session", required=True)
    monitor.add_argument("--thread-id", required=True)
    monitor.add_argument("--primary-model", default="gpt-5.6-sol")
    monitor.add_argument("--primary-reasoning-effort", default="max")
    monitor.add_argument("--compact-model", default="gpt-5.6-luna")
    monitor.add_argument("--compact-reasoning-effort", default="xhigh")
    monitor.add_argument("--codex-args-json", default="[]")
    monitor.add_argument("--resume-prompt", default=DEFAULT_RESUME_PROMPT)
    monitor.add_argument("--cooldown-seconds", type=int, default=300)
    monitor.add_argument("--max-recoveries", type=int, default=0)
    monitor.add_argument("--compact-wait-seconds", type=int, default=600)

    guardian = subparsers.add_parser(
        "guardian", help="supervise and restore the tmux output monitor"
    )
    guardian.add_argument(
        "--version",
        action="version",
        version=f"codex-watch-guardian {__version__}",
    )
    guardian.add_argument("--session", default="codex-goal")
    guardian.add_argument("--poll-seconds", type=float, default=5)

    args = parser.parse_args(argv)
    if args.command == "guardian":
        run_guardian(args.session, poll_seconds=args.poll_seconds)
        return 0
    if args.command == "monitor":
        config = RecoveryConfig(
            thread_id=validate_thread_id(args.thread_id),
            primary_model=args.primary_model,
            primary_reasoning_effort=args.primary_reasoning_effort,
            compact_model=args.compact_model,
            compact_reasoning_effort=args.compact_reasoning_effort,
            codex_args=tuple(json.loads(args.codex_args_json)),
            cooldown_seconds=args.cooldown_seconds,
            max_recoveries=args.max_recoveries,
            compact_wait_seconds=args.compact_wait_seconds,
            resume_prompt=args.resume_prompt,
        )
        monitor_stdin(args.session, config)
        return 0

    root_dir = Path(__file__).resolve().parents[1]
    working_dir = Path.cwd().resolve()
    log_path = args.log_path or str(default_log_path())
    codex_args = args.codex_args
    if codex_args and codex_args[0] == "--":
        codex_args = codex_args[1:]
    codex_args = normalize_codex_args(codex_args, safe_mode=args.safe)
    session_exists = tmux_session_exists(args.session)
    thread_id = validate_thread_id(args.thread_id) if args.thread_id else None
    should_resume = args.resume or thread_id is not None

    if session_exists:
        pinned_thread_id = tmux_get_thread_id(args.session)
        thread_id = thread_id or pinned_thread_id
        if thread_id is None:
            if args.dry_run:
                thread_id = "00000000-0000-0000-0000-000000000000"
            else:
                raise SystemExit(
                    "existing tmux session has no pinned Codex thread ID; "
                    "restart with --thread-id <UUID>"
                )
    elif should_resume and thread_id is None:
        thread_id = find_latest_thread_id(cwd=working_dir)
        if thread_id is None and not args.dry_run:
            raise SystemExit(f"no Codex thread found for {working_dir}")

    started_after = datetime.now(timezone.utc)
    codex_command = build_codex_command(
        model=args.primary_model,
        reasoning_effort=args.primary_reasoning_effort,
        codex_args=codex_args,
        resume_thread_id=thread_id if should_resume else None,
    )

    def run_command(command: list[str]) -> None:
        if args.dry_run:
            print(shlex.join(command))
            return
        subprocess.run(command, check=True)

    if not session_exists:
        run_command(tmux_new_session_command(args.session, codex_command))
        if thread_id is None:
            if args.dry_run:
                thread_id = "00000000-0000-0000-0000-000000000000"
            else:
                thread_id = wait_for_new_thread_id(
                    cwd=working_dir,
                    started_after=started_after,
                )
                if thread_id is None:
                    raise SystemExit(
                        "Codex started but its thread ID could not be detected; "
                        "rerun with --thread-id <UUID>"
                    )

    assert thread_id is not None
    if not args.dry_run:
        run_command(tmux_set_thread_id_command(args.session, thread_id))
        option_values = {
            "@codex_primary_model": args.primary_model,
            "@codex_primary_effort": args.primary_reasoning_effort,
            "@codex_compact_model": args.compact_model,
            "@codex_compact_effort": args.compact_reasoning_effort,
            "@codex_args_json": json.dumps(codex_args),
            "@codex_cooldown_seconds": str(args.cooldown_seconds),
            "@codex_max_recoveries": str(args.max_recoveries),
            "@codex_recovery_count": "0",
            "@codex_compact_wait_seconds": str(args.compact_wait_seconds),
            "@codex_log_path": log_path,
            "@codex_resume_prompt": args.resume_prompt,
        }
        for name, value in option_values.items():
            run_command(tmux_set_option_command(args.session, name, value))
    pipe_command = monitor_pipe_command(
        root_dir=str(root_dir),
        session=args.session,
        thread_id=thread_id,
        primary_model=args.primary_model,
        primary_reasoning_effort=args.primary_reasoning_effort,
        compact_model=args.compact_model,
        compact_reasoning_effort=args.compact_reasoning_effort,
        codex_args=codex_args,
        resume_prompt=args.resume_prompt,
        log_path=log_path,
        cooldown_seconds=args.cooldown_seconds,
        max_recoveries=args.max_recoveries,
        compact_wait_seconds=args.compact_wait_seconds,
    )

    commands = [tmux_pipe_pane_command(args.session, pipe_command)]
    if not args.no_attach:
        commands.append(tmux_attach_command(args.session))

    for command in commands:
        run_command(command)
    return 0


def start_main() -> int:
    return main(["start", *sys.argv[1:]])


def guardian_main() -> int:
    return main(["guardian", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
