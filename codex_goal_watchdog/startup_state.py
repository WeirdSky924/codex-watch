"""Resolve restart options from one watchdog session and its pinned rollout."""

from __future__ import annotations

from argparse import Namespace

from .bindings import SessionBinding
from .sessions import find_latest_thread_execution_profile


STRING_OPTIONS = (
    "primary_model",
    "primary_reasoning_effort",
    "compact_model",
    "compact_reasoning_effort",
    "resume_prompt",
    "log_path",
)
INT_OPTIONS = (
    "cooldown_seconds",
    "max_recoveries",
    "compact_wait_seconds",
    "thread_max_compactions",
    "thread_max_rollout_bytes",
    "thread_max_context_tokens",
    "thread_no_progress_tokens",
    "thread_no_event_seconds",
    "thread_health_poll_seconds",
    "thread_max_repeated_content",
    "thread_max_repeated_commands",
)


def _provided(argv: list[str], option: str) -> bool:
    flag = "--" + option.replace("_", "-")
    for arg in argv[1:]:
        if arg == "--":
            break
        if arg == flag or arg.startswith(flag + "="):
            return True
    return False


def restore_startup_options(
    args: Namespace,
    argv: list[str],
    binding: SessionBinding | None,
    thread_id: str | None,
) -> None:
    if args.new or thread_id is None:
        return
    pinned_binding = (
        binding
        if binding is not None and binding.thread_id == thread_id
        else None
    )
    saved = pinned_binding.launch_options if pinned_binding is not None else {}
    for key in STRING_OPTIONS:
        value = saved.get(key)
        if not _provided(argv, key) and isinstance(value, str) and (
            value or key == "log_path"
        ):
            setattr(args, key, value)
    for key in INT_OPTIONS:
        value = saved.get(key)
        if not _provided(argv, key) and type(value) is int and value >= 0:
            setattr(args, key, value)
    if (
        not (_provided(argv, "safe") or _provided(argv, "unsafe"))
        and saved.get("safe") is True
    ):
        args.safe = True
    raw_args = saved.get("codex_args")
    if (
        pinned_binding is not None
        and not args.codex_args
        and "--" not in argv
        and isinstance(raw_args, list)
        and all(isinstance(item, str) for item in raw_args)
    ):
        args.codex_args = list(raw_args)

    profile = find_latest_thread_execution_profile(
        thread_id=thread_id,
        offset=(
            pinned_binding.launch_profile_offset
            if pinned_binding is not None
            else 0
        ),
    )
    if profile is not None:
        if not _provided(argv, "primary_model"):
            args.primary_model = profile.model
        if not _provided(argv, "primary_reasoning_effort"):
            args.primary_reasoning_effort = profile.reasoning_effort


def snapshot_launch_options(
    args: Namespace, raw_codex_args: list[str]
) -> dict[str, object]:
    snapshot = {
        key: getattr(args, key)
        for key in (*STRING_OPTIONS, *INT_OPTIONS, "safe")
    }
    snapshot["codex_args"] = list(raw_codex_args)
    return snapshot
