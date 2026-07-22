"""Recovery state machine for Codex upstream stalls."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass

from .launcher import build_codex_command


DEFAULT_STALL_PATTERN = (
    "codex upstream stalled: no real data for 5m0s, connection recycled"
)
CONTEXT_WINDOW_EXHAUSTED_PATTERN = (
    "Codex ran out of room in the model's context window. "
    "Start a new thread or clear earlier history before retrying."
)
COMPACTION_RECOVERY_REASONS = {
    "codex_upstream_stalled",
    "context_window_exhausted",
}
DEFAULT_RESUME_PROMPT = (
    "继续刚才被 5m0s 中断的 goal。从当前仓库状态和最近上下文继续，"
    "不要重复已经完成的操作；先检查现状，再推进未完成步骤。"
)
RETRYABLE_HTTP_CODES = (402, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524)
RETRYABLE_HTTP_RE = re.compile(
    r"\b(" + "|".join(str(code) for code in RETRYABLE_HTTP_CODES) + r")\b"
)
RETRYABLE_NETWORK_RE = re.compile(
    r"connection (?:reset|closed|recycled)|broken pipe|gateway timeout|"
    r"upstream connect error|error sending request|request timed out|"
    r"timed out waiting for|unexpected eof",
    re.IGNORECASE,
)


def _is_retryable_upstream_error(text: str) -> bool:
    try:
        payload, _ = json.JSONDecoder().raw_decode(text.lstrip())
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    return isinstance(error, dict) and (
        error.get("message") == "Upstream request failed"
        and error.get("type") == "upstream_error"
    )


def classify_recovery_reason(text: str) -> str | None:
    """Classify only Codex TUI fatal-error rows, not ordinary transcript text."""
    if "■" not in text:
        return None
    for segment in text.split("■")[1:]:
        error_text = segment[:1200]
        if DEFAULT_STALL_PATTERN in error_text:
            return "codex_upstream_stalled"
        if CONTEXT_WINDOW_EXHAUSTED_PATTERN in error_text:
            return "context_window_exhausted"
        if _is_retryable_upstream_error(error_text):
            return "retryable_upstream_error"
        status = RETRYABLE_HTTP_RE.search(error_text)
        error_shaped = re.search(
            r"unexpected status|too many requests|bad gateway|gateway timeout|"
            r"service unavailable|stream disconnected|request failed|upstream error|"
            r"cloudflare",
            error_text,
            re.IGNORECASE,
        )
        if status and error_shaped:
            return f"retryable_http_{status.group(1)}"
        if RETRYABLE_NETWORK_RE.search(error_text):
            return "retryable_network"
    return None


@dataclass(frozen=True)
class RecoveryConfig:
    thread_id: str = ""
    primary_model: str = "gpt-5.6-sol"
    primary_reasoning_effort: str = "max"
    compact_model: str = "gpt-5.6-luna"
    compact_reasoning_effort: str = "xhigh"
    codex_args: tuple[str, ...] = ()
    stall_pattern: str = DEFAULT_STALL_PATTERN
    cooldown_seconds: int = 300
    max_recoveries: int = 0
    abort_delay_seconds: int = 2
    quit_wait_seconds: int = 3
    startup_wait_seconds: int = 5
    model_switch_delay_seconds: int = 2
    compact_wait_seconds: int = 600
    resume_prompt: str = DEFAULT_RESUME_PROMPT


@dataclass(frozen=True)
class RecoveryEvent:
    reason: str
    observed_at: float
    line: str


@dataclass(frozen=True)
class RecoveryStep:
    kind: str
    value: str
    timeout_seconds: float | None = None


class RecoveryController:
    """Detects recoverable stalls while preventing recovery loops."""

    def __init__(
        self,
        config: RecoveryConfig,
        *,
        initial_recovery_count: int = 0,
    ) -> None:
        self.config = config
        self.recovery_count = max(0, initial_recovery_count)

    def observe(self, line: str, now: float) -> RecoveryEvent | None:
        reason = classify_recovery_reason(line)
        if reason is None and "■" in line and self.config.stall_pattern in line:
            reason = "codex_upstream_stalled"
        if reason is None:
            return None
        if (
            self.config.max_recoveries > 0
            and self.recovery_count >= self.config.max_recoveries
        ):
            return None
        self.recovery_count += 1
        return RecoveryEvent(
            reason=reason,
            observed_at=now,
            line=line,
        )


def build_startup_update_steps(
    codex_command: list[str], expected_version: str
) -> list[RecoveryStep]:
    """Install a startup update before Codex has created a thread."""
    return [
        RecoveryStep("key", "1"),
        RecoveryStep("wait_shell", "300"),
        RecoveryStep("ensure_codex_version", expected_version),
        RecoveryStep("text", shlex.join(codex_command)),
        RecoveryStep("wait_codex", "30"),
    ]


def build_codex_update_completion_steps(
    config: RecoveryConfig, expected_version: str
) -> list[RecoveryStep]:
    """Verify an updater result, then restore the pinned Codex thread."""
    if not config.thread_id:
        raise ValueError("Codex update recovery requires a pinned thread ID")
    primary_command = shlex.join(
        build_codex_command(
            model=config.primary_model,
            reasoning_effort=config.primary_reasoning_effort,
            codex_args=config.codex_args,
            resume_thread_id=config.thread_id,
        )
    )
    return [
        RecoveryStep("wait_shell", "300"),
        RecoveryStep("ensure_codex_version", expected_version),
        RecoveryStep("text", primary_command),
        RecoveryStep("wait_codex", "30"),
        RecoveryStep("sleep", str(config.startup_wait_seconds)),
        RecoveryStep("resume_goal_or_prompt", config.resume_prompt),
    ]


def build_codex_update_steps(
    config: RecoveryConfig, expected_version: str
) -> list[RecoveryStep]:
    """Accept the official updater and restore only after version verification."""
    return [
        RecoveryStep("key", "1"),
        *build_codex_update_completion_steps(config, expected_version),
    ]


def build_post_update_restart_steps(config: RecoveryConfig) -> list[RecoveryStep]:
    """Restart the pinned thread after the Codex updater returns to the shell."""
    if not config.thread_id:
        raise ValueError("post-update restart requires a pinned Codex thread ID")
    primary_command = shlex.join(
        build_codex_command(
            model=config.primary_model,
            reasoning_effort=config.primary_reasoning_effort,
            codex_args=config.codex_args,
            resume_thread_id=config.thread_id,
        )
    )
    return [
        RecoveryStep("update_codex", ""),
        RecoveryStep("text", primary_command),
        RecoveryStep("wait_codex", "30"),
        RecoveryStep("sleep", str(config.startup_wait_seconds)),
        RecoveryStep("resume_goal_or_prompt", config.resume_prompt),
    ]


def build_recovery_steps(
    config: RecoveryConfig,
    *,
    reason: str = "codex_upstream_stalled",
    recovery_attempt: int = 1,
) -> list[RecoveryStep]:
    """Build tmux actions for model fallback, compaction, and resume."""
    if not config.thread_id:
        raise ValueError("recovery requires a pinned Codex thread ID")
    restart_delay = config.cooldown_seconds if recovery_attempt > 1 else 0
    compact_command = shlex.join(
        build_codex_command(
            model=config.compact_model,
            reasoning_effort=config.compact_reasoning_effort,
            codex_args=config.codex_args,
            resume_thread_id=config.thread_id,
        )
    )
    primary_command = shlex.join(
        build_codex_command(
            model=config.primary_model,
            reasoning_effort=config.primary_reasoning_effort,
            codex_args=config.codex_args,
            resume_thread_id=config.thread_id,
        )
    )
    if reason not in COMPACTION_RECOVERY_REASONS:
        return [
            RecoveryStep("key", "C-c"),
            RecoveryStep("sleep", str(config.abort_delay_seconds)),
            RecoveryStep("text", "/quit"),
            RecoveryStep("wait_shell", "30"),
            RecoveryStep("sleep", str(max(0, restart_delay))),
            RecoveryStep("text", primary_command),
            RecoveryStep("wait_codex", "30"),
            RecoveryStep("sleep", str(config.startup_wait_seconds)),
            RecoveryStep("resume_goal_or_prompt", config.resume_prompt),
        ]
    return [
        RecoveryStep("key", "C-c"),
        RecoveryStep("sleep", str(config.abort_delay_seconds)),
        RecoveryStep("text", "/quit"),
        RecoveryStep("wait_shell", "30"),
        RecoveryStep("sleep", str(max(0, restart_delay))),
        RecoveryStep("text", compact_command),
        RecoveryStep("wait_codex", "30"),
        RecoveryStep("sleep", str(config.startup_wait_seconds)),
        RecoveryStep("leave_goal_paused", ""),
        RecoveryStep("mark_compaction", config.thread_id),
        RecoveryStep("text", "/compact"),
        RecoveryStep(
            "wait_compaction",
            config.thread_id,
            timeout_seconds=config.compact_wait_seconds,
        ),
        RecoveryStep("text", "/quit"),
        RecoveryStep("wait_shell", "30"),
        RecoveryStep("sleep", "1"),
        RecoveryStep("text", primary_command),
        RecoveryStep("wait_codex", "30"),
        RecoveryStep("sleep", str(config.startup_wait_seconds)),
        RecoveryStep("resume_goal_or_prompt", config.resume_prompt),
    ]
