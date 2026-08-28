"""Recovery state machine for Codex upstream stalls."""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass

from .launcher import build_codex_command


DEFAULT_STALL_PATTERN = (
    "codex upstream stalled: no real data for 5m0s, connection recycled"
)
MODEL_AT_CAPACITY_PATTERN = (
    "Selected model is at capacity. Please try a different model"
)
SERVERS_OVERLOADED_PATTERN = (
    "Our servers are currently overloaded. Please try again later."
)
UPSTREAM_ACCESS_DENIED_PATTERN = "Upstream access denied"
THREAD_HEALTH_ROTATION_REASON = "thread_health_rotation"
THREAD_ROTATION_RECOVERY_REASONS = {
    "upstream_access_denied",
    THREAD_HEALTH_ROTATION_REASON,
}
COMPACTION_RECOVERY_REASONS = {
    "codex_upstream_stalled",
}
DEFAULT_RESUME_PROMPT = (
    "继续刚才被 5m0s 中断的 goal。从当前仓库状态和最近上下文继续，"
    "不要重复已经完成的操作；先检查现状，再推进未完成步骤。"
)


def build_thread_rotation_prompt(
    goal_objective: str | None,
    *,
    resume_goal: bool,
    rotation_reason: str = "upstream_access_denied",
    handoff_path: str | None = None,
) -> str:
    objective = goal_objective or (
        "未能从旧 thread 的 rollout 提取 Goal Objective。请从最新工作树、"
        "唯一 ACTIVE Plan 和项目 canonical 恢复文档识别仍在进行的目标。"
    )
    state_instruction = (
        "上一 Goal 处于 blocked 人工审核态。重新创建 Goal 只用于保留目标与恢复"
        "上下文，不得继续产品执行或绕过审核；保持 blocked 边界并等待用户明确处理。"
        if not resume_goal
        else "上一 Goal 可继续执行；完成状态校准后从最新可执行入口接力推进。"
    )
    encoded_objective = json.dumps(objective, ensure_ascii=False)
    reason_text = (
        "upstream access denied 被上游隔离"
        if rotation_reason == "upstream_access_denied"
        else f"watchdog thread-health 阈值触发（{rotation_reason}）"
    )
    handoff_instruction = (
        "watchdog 已生成主机本地 bounded handoff，必须先读取并与更高优先级证据"
        f"核对：{json.dumps(handoff_path, ensure_ascii=False)}。"
        if handoff_path
        else ""
    )
    return (
        f"上一 Codex thread 因 {reason_text}，禁止恢复或重试"
        "旧 thread。请创建一个不设置 token budget 的新 Goal，并持续接力执行。"
        f"{handoff_instruction}"
        "上一 Goal Objective 原文采用 JSON 字符串无损编码，解码后原样使用："
        f"{encoded_objective}。"
        "恢复前必须重新核对状态，优先级为：最新用户要求 > 当前工作树 > 唯一 "
        "ACTIVE Plan State 及 current executable entry > canonical 规则/规范 > "
        "handoff 缓存。计划或 handoff 可能滞后；发生冲突时使用更高优先级证据，"
        "不得恢复历史授权、旧 checkpoint 或重复已完成操作。先检查现状，再用上述"
        f" Objective 原文创建 Goal。{state_instruction}"
    )
RETRYABLE_HTTP_CODES = (
    401,
    402,
    429,
    500,
    502,
    503,
    504,
    520,
    521,
    522,
    523,
    524,
)
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


def classify_recovery_message(message: str) -> str | None:
    """Classify one structured task failure without terminal row markers."""
    if MODEL_AT_CAPACITY_PATTERN in message:
        return "model_at_capacity"
    if SERVERS_OVERLOADED_PATTERN in message:
        return "servers_overloaded"
    if DEFAULT_STALL_PATTERN in message:
        return "codex_upstream_stalled"
    if UPSTREAM_ACCESS_DENIED_PATTERN.lower() in message.lower():
        return "upstream_access_denied"
    if _is_retryable_upstream_error(message):
        return "retryable_upstream_error"
    status = RETRYABLE_HTTP_RE.search(message)
    error_shaped = re.search(
        r"unexpected status|too many requests|bad gateway|gateway timeout|"
        r"service unavailable|stream disconnected|request failed|upstream error|"
        r"api disabl(?:e|ed)|cloudflare",
        message,
        re.IGNORECASE,
    )
    if status and error_shaped:
        return f"retryable_http_{status.group(1)}"
    if RETRYABLE_NETWORK_RE.search(message):
        return "retryable_network"
    return None


def classify_recovery_reason(text: str) -> str | None:
    """Classify Codex TUI terminal errors, not ordinary transcript text."""
    markers = list(re.finditer(r"[⚠■]", text))
    for index in range(len(markers) - 1, -1, -1):
        marker = markers[index]
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        reason = classify_recovery_message(text[marker.end() : end].lstrip()[:1200])
        if marker.group() == "⚠" and reason != "model_at_capacity":
            continue
        if reason is not None:
            return reason
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
    thread_max_compactions: int = 3
    thread_max_rollout_bytes: int = 512 * 1024 * 1024
    # Retained only so older launchers and tmux options remain parseable.
    # Context-window management belongs entirely to Codex and this value is
    # never used by watchdog recovery decisions.
    thread_max_context_tokens: int = 0
    thread_no_progress_tokens: int = 1_000_000
    thread_no_event_seconds: int = 30 * 60
    thread_health_poll_seconds: int = 30
    thread_max_repeated_content: int = 3
    thread_max_repeated_commands: int = 3


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


class IncidentLogAggregator:
    """Emit the first duplicate incident and one bounded count summary."""

    def __init__(self, emit: Callable[[str], None]) -> None:
        self.emit = emit
        self.key: str | None = None
        self.summary = ""
        self.count = 0

    def record(self, *, key: str, first: str, summary: str) -> None:
        if self.key != key:
            self.flush()
            self.key = key
            self.summary = summary
            self.count = 1
            self.emit(first)
            return
        self.count += 1

    def flush(self) -> None:
        if self.key is not None and self.count > 1:
            self.emit(f"{self.summary}; suppressed={self.count - 1}")
        self.key = None
        self.summary = ""
        self.count = 0


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
        return self.begin(reason=reason, now=now, line=line)

    def begin(self, *, reason: str, now: float, line: str = "") -> RecoveryEvent | None:
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

    def reset_after_verified_success(self) -> None:
        self.recovery_count = 0


def thread_rotation_reason(
    config: RecoveryConfig,
    *,
    compaction_count: int,
    rollout_bytes: int,
    context_tokens: int,
    total_tokens: int,
    tokens_at_last_progress: int,
    last_event_age_seconds: float,
    repeated_content_count: int = 0,
    repeated_command_count: int = 0,
) -> str | None:
    """Return watchdog-owned health failures.

    context_tokens remains an argument for compatibility with older callers
    and telemetry snapshots, but Codex exclusively owns context compaction
    and context-window limits. It is intentionally not a trigger.
    """
    checks = (
        (
            config.thread_max_compactions,
            compaction_count,
            "max_compactions",
        ),
        (
            config.thread_max_rollout_bytes,
            rollout_bytes,
            "max_rollout_bytes",
        ),
        (
            config.thread_max_repeated_content,
            repeated_content_count,
            "repeated_content",
        ),
        (
            config.thread_max_repeated_commands,
            repeated_command_count,
            "repeated_command",
        ),
        (
            config.thread_no_progress_tokens,
            max(0, total_tokens - tokens_at_last_progress),
            "no_progress_tokens",
        ),
        (
            config.thread_no_event_seconds,
            last_event_age_seconds,
            "no_rollout_events",
        ),
    )
    for threshold, actual, reason in checks:
        if threshold > 0 and actual >= threshold:
            return reason
    return None


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
    config: RecoveryConfig,
    expected_version: str,
    *,
    resume_goal: bool = True,
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
        _goal_recovery_step(config, resume_goal=resume_goal),
    ]


def build_codex_update_steps(
    config: RecoveryConfig,
    expected_version: str,
    *,
    resume_goal: bool = True,
) -> list[RecoveryStep]:
    """Accept the official updater and restore only after version verification."""
    return [
        RecoveryStep("key", "1"),
        *build_codex_update_completion_steps(
            config,
            expected_version,
            resume_goal=resume_goal,
        ),
    ]


def build_post_update_restart_steps(
    config: RecoveryConfig, *, resume_goal: bool = True
) -> list[RecoveryStep]:
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
        _goal_recovery_step(config, resume_goal=resume_goal),
    ]


def _goal_recovery_step(
    config: RecoveryConfig,
    *,
    resume_goal: bool,
    resume_stalled_goal: bool = False,
) -> RecoveryStep:
    if not resume_goal:
        return RecoveryStep("leave_goal_paused", "")
    if resume_stalled_goal:
        return RecoveryStep("resume_stalled_goal_or_prompt", config.resume_prompt)
    return RecoveryStep("resume_goal_or_prompt", config.resume_prompt)


def build_recovery_steps(
    config: RecoveryConfig,
    *,
    reason: str = "codex_upstream_stalled",
    recovery_attempt: int = 1,
    resume_goal: bool = True,
    resume_stalled_goal: bool = False,
    goal_objective: str | None = None,
    handoff_path: str | None = None,
    rotation_detail: str | None = None,
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
    if reason in THREAD_ROTATION_RECOVERY_REASONS:
        fresh_command = shlex.join(
            build_codex_command(
                model=config.primary_model,
                reasoning_effort=config.primary_reasoning_effort,
                codex_args=config.codex_args,
            )
        )
        return [
            RecoveryStep("key", "C-c"),
            RecoveryStep("sleep", str(config.abort_delay_seconds)),
            RecoveryStep("text", "/quit"),
            RecoveryStep("wait_shell", "30"),
            RecoveryStep("sleep", str(max(0, restart_delay))),
            RecoveryStep("text", fresh_command),
            RecoveryStep("wait_codex", "30"),
            RecoveryStep("sleep", str(config.startup_wait_seconds)),
            RecoveryStep(
                "text",
                build_thread_rotation_prompt(
                    goal_objective,
                    resume_goal=resume_goal,
                    rotation_reason=(
                        rotation_detail or reason
                    ),
                    handoff_path=handoff_path,
                ),
            ),
        ]
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
            _goal_recovery_step(
                config,
                resume_goal=resume_goal,
                resume_stalled_goal=resume_stalled_goal,
            ),
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
        _goal_recovery_step(
            config,
            resume_goal=resume_goal,
            resume_stalled_goal=resume_stalled_goal,
        ),
    ]
