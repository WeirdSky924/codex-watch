"""Monitor tmux pipe output and trigger Codex recovery."""

from __future__ import annotations

import codecs
import re
import sys
import time
from collections.abc import Callable, Iterable
from typing import BinaryIO

from .recovery import RecoveryConfig, build_recovery_steps
from .tmux_control import execute_steps


ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x1b\x07]*(?:\x07|\x1b\\)|[@-_])"
)
ROLLING_BUFFER_SIZE = 8192


def normalize_terminal_text(value: str) -> str:
    """Remove terminal control sequences and normalize visual line wrapping."""
    return " ".join(ANSI_ESCAPE_RE.sub("", value).split())


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
    log: Callable[[str], None] | None = None,
) -> None:
    from .recovery import RecoveryController

    controller = RecoveryController(config)
    emit = log or (lambda message: print(message, flush=True))

    def default_execute(tmux_target: str, steps: list) -> None:
        execute_steps(tmux_target, steps)

    run_execute = execute or default_execute
    rolling_output = ""
    for line in lines:
        rolling_output = normalize_terminal_text(f"{rolling_output} {line}")
        rolling_output = rolling_output[-ROLLING_BUFFER_SIZE:]
        event = controller.observe(rolling_output, now=now())
        if event is None:
            continue
        rolling_output = ""
        emit(
            f"[codex-goal-watchdog] recovery #{controller.recovery_count}: "
            f"{event.reason}"
        )
        run_execute(target, build_recovery_steps(config, reason=event.reason))


def monitor_stdin(target: str, config: RecoveryConfig) -> None:
    print(f"[codex-goal-watchdog] monitor started: target={target}", flush=True)
    run_monitor(
        lines=iter_decoded_chunks(sys.stdin.buffer),
        target=target,
        config=config,
    )
