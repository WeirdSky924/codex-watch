"""Resolve Codex rollout metadata to stable thread IDs."""

from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID


DEFAULT_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
DEFAULT_SHELL_SNAPSHOTS_ROOT = Path.home() / ".codex" / "shell_snapshots"


@dataclass(frozen=True)
class SessionRecord:
    path: Path
    thread_id: str
    cwd: Path
    started_at: datetime
    modified_at: float
    source: object


@dataclass(frozen=True)
class TaskFailure:
    incident_id: str
    message: str
    codex_error_info: str | None


@dataclass(frozen=True)
class ThreadTelemetry:
    thread_id: str
    rollout_path: Path
    rollout_bytes: int
    total_tokens: int
    context_tokens: int
    context_window: int
    compaction_count: int
    tokens_at_last_progress: int
    last_event_at: float
    last_progress_at: float
    verified_event_count: int = 0
    progress_event_count: int = 0
    latest_failure: TaskFailure | None = None


VERIFIED_GOAL_STATES = {"achieved", "complete", "completed"}


def validate_thread_id(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise ValueError(f"invalid Codex thread ID: {value}") from exc


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_session_record(path: Path) -> SessionRecord | None:
    try:
        with path.open("r", encoding="utf-8") as stream:
            first_line = stream.readline()
        event = json.loads(first_line)
        if event.get("type") != "session_meta":
            return None
        payload = event["payload"]
        thread_id = validate_thread_id(payload.get("id") or payload["session_id"])
        cwd = Path(payload["cwd"]).resolve()
        timestamp = payload.get("timestamp") or event["timestamp"]
        return SessionRecord(
            path=path,
            thread_id=thread_id,
            cwd=cwd,
            started_at=_parse_timestamp(timestamp),
            modified_at=path.stat().st_mtime,
            source=payload.get("source", "cli"),
        )
    except (OSError, KeyError, TypeError, json.JSONDecodeError, ValueError):
        return None


def _session_records(sessions_root: Path) -> list[SessionRecord]:
    if not sessions_root.exists():
        return []
    records = []
    for path in sessions_root.rglob("*.jsonl"):
        record = _read_session_record(path)
        if record is not None:
            records.append(record)
    return records


def find_latest_thread_id(
    *, cwd: Path, sessions_root: Path = DEFAULT_SESSIONS_ROOT
) -> str | None:
    resolved_cwd = cwd.resolve()
    matches = [
        record
        for record in _session_records(sessions_root)
        if record.cwd == resolved_cwd
    ]
    if not matches:
        return None
    return max(matches, key=lambda record: record.modified_at).thread_id


def _process_tree_pids(*, pane_pid: int, proc_root: Path) -> set[int]:
    discovered: set[int] = set()
    pending = [pane_pid]
    while pending:
        pid = pending.pop()
        if pid in discovered:
            continue
        discovered.add(pid)
        children_path = proc_root / str(pid) / "task" / str(pid) / "children"
        try:
            children = children_path.read_text(encoding="utf-8").split()
        except OSError:
            continue
        for value in children:
            try:
                pending.append(int(value))
            except ValueError:
                continue
    return discovered


def find_active_cli_thread_id(
    *,
    pane_pid: int,
    cwd: Path,
    proc_root: Path = Path("/proc"),
) -> str | None:
    """Find the newest top-level CLI rollout opened by a tmux pane process."""
    resolved_cwd = cwd.resolve()
    records: dict[str, SessionRecord] = {}
    for pid in _process_tree_pids(pane_pid=pane_pid, proc_root=proc_root):
        fd_root = proc_root / str(pid) / "fd"
        try:
            descriptors = tuple(fd_root.iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                path = descriptor.resolve(strict=True)
            except OSError:
                continue
            if not path.name.startswith("rollout-") or path.suffix != ".jsonl":
                continue
            record = _read_session_record(path)
            if (
                record is not None
                and record.cwd == resolved_cwd
                and record.source == "cli"
            ):
                records[record.thread_id] = record
    if not records:
        return None
    return max(
        records.values(),
        key=lambda record: (record.started_at, record.modified_at),
    ).thread_id


def find_new_thread_id(
    *,
    cwd: Path,
    started_after: datetime,
    sessions_root: Path = DEFAULT_SESSIONS_ROOT,
    shell_snapshots_root: Path | None = None,
) -> str | None:
    resolved_cwd = cwd.resolve()
    threshold = started_after.astimezone(timezone.utc)
    matches = [
        record
        for record in _session_records(sessions_root)
        if record.cwd == resolved_cwd and record.started_at >= threshold
    ]
    if not matches:
        return _find_new_shell_snapshot_thread_id(
            started_after=threshold,
            shell_snapshots_root=shell_snapshots_root,
        )
    return max(matches, key=lambda record: record.started_at).thread_id


def _find_new_shell_snapshot_thread_id(
    *,
    started_after: datetime,
    shell_snapshots_root: Path | None,
) -> str | None:
    if shell_snapshots_root is None or not shell_snapshots_root.exists():
        return None
    threshold = started_after.timestamp()
    matches: list[tuple[float, str]] = []
    for path in shell_snapshots_root.glob("*.sh"):
        thread_id_text, separator, _rest = path.name.partition(".")
        if not separator:
            continue
        try:
            thread_id = validate_thread_id(thread_id_text)
            modified_at = path.stat().st_mtime
        except (OSError, ValueError):
            continue
        if modified_at >= threshold:
            matches.append((modified_at, thread_id))
    if not matches:
        return None
    return max(matches)[1]


def wait_for_new_thread_id(
    *,
    cwd: Path,
    started_after: datetime,
    sessions_root: Path = DEFAULT_SESSIONS_ROOT,
    shell_snapshots_root: Path | None = None,
    timeout_seconds: float = 15,
    on_wait: Callable[[], bool] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> str | None:
    deadline = now() + timeout_seconds
    while now() < deadline:
        thread_id = find_new_thread_id(
            cwd=cwd,
            started_after=started_after,
            sessions_root=sessions_root,
            shell_snapshots_root=shell_snapshots_root,
        )
        if thread_id:
            return thread_id
        if on_wait is not None and on_wait():
            deadline = now() + timeout_seconds
        sleeper(0.1)
    return None


def find_thread_rollout_path(
    *, thread_id: str, sessions_root: Path = DEFAULT_SESSIONS_ROOT
) -> Path | None:
    normalized = validate_thread_id(thread_id)
    for record in _session_records(sessions_root):
        if record.thread_id == normalized:
            return record.path
    return None


def _nonnegative_int(value: object) -> int:
    return max(0, value) if type(value) is int else 0


def _event_epoch(event: dict, *, fallback: float) -> float:
    timestamp = event.get("timestamp")
    if not isinstance(timestamp, str):
        return fallback
    try:
        return _parse_timestamp(timestamp).timestamp()
    except (TypeError, ValueError):
        return fallback


class ThreadTelemetryTracker:
    """Incrementally summarize one rollout without rescanning it on every tick."""

    def __init__(
        self,
        *,
        thread_id: str,
        sessions_root: Path = DEFAULT_SESSIONS_ROOT,
    ) -> None:
        self.thread_id = validate_thread_id(thread_id)
        self.sessions_root = sessions_root
        self._path: Path | None = None
        self._offset = 0
        self._partial_line_start: int | None = None
        self._reset_metrics()

    def _reset_metrics(self) -> None:
        self.total_tokens = 0
        self.context_tokens = 0
        self.context_window = 0
        self.compaction_count = 0
        self.tokens_at_last_progress = 0
        self.last_event_at = 0.0
        self.last_progress_at = 0.0
        self._progress_pending = False
        self.verified_event_count = 0
        self.progress_event_count = 0
        self.latest_failure: TaskFailure | None = None

    def _consume_event(self, event: dict, *, fallback_time: float) -> None:
        observed_at = _event_epoch(event, fallback=fallback_time)
        self.last_event_at = max(self.last_event_at, observed_at)
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        payload_type = payload.get("type")

        compacted = event.get("type") == "compacted" or (
            payload_type == "context_compacted"
        )
        if compacted:
            self.compaction_count += 1

        successful_task = payload_type == "task_complete" and not isinstance(
            payload.get("error"), dict
        )
        is_progress = compacted or successful_task
        goal = payload.get("goal")
        goal = goal if isinstance(goal, dict) else {}
        is_verified_state = is_progress or (
            payload_type == "thread_goal_updated"
            and goal.get("status") in VERIFIED_GOAL_STATES
        )
        if is_verified_state:
            self.verified_event_count += 1
        if is_progress:
            self.progress_event_count += 1
            self.last_progress_at = max(self.last_progress_at, observed_at)
            self.tokens_at_last_progress = self.total_tokens
            self._progress_pending = True

        if payload_type == "task_complete":
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                incident_id = payload.get("turn_id") or event.get("timestamp")
                if isinstance(incident_id, str) and incident_id:
                    error_info = error.get("codex_error_info")
                    self.latest_failure = TaskFailure(
                        incident_id=incident_id,
                        message=error["message"],
                        codex_error_info=(
                            error_info if isinstance(error_info, str) else None
                        ),
                    )

        if payload_type != "token_count":
            return
        info = payload.get("info")
        info = info if isinstance(info, dict) else {}
        total_usage = info.get("total_token_usage")
        total_usage = total_usage if isinstance(total_usage, dict) else {}
        last_usage = info.get("last_token_usage")
        last_usage = last_usage if isinstance(last_usage, dict) else {}
        self.total_tokens = _nonnegative_int(total_usage.get("total_tokens"))
        self.context_tokens = _nonnegative_int(last_usage.get("total_tokens"))
        self.context_window = _nonnegative_int(info.get("model_context_window"))
        if self._progress_pending:
            self.tokens_at_last_progress = self.total_tokens
            self._progress_pending = False

    def snapshot(self) -> ThreadTelemetry | None:
        path = self._path
        if path is None or not path.exists():
            path = find_thread_rollout_path(
                thread_id=self.thread_id,
                sessions_root=self.sessions_root,
            )
            if path is None:
                return None
            self._path = path
            self._offset = 0
            self._reset_metrics()

        try:
            file_stat = path.stat()
            if file_stat.st_size < self._offset:
                self._offset = 0
                self._partial_line_start = None
                self._reset_metrics()
            with path.open("r", encoding="utf-8") as stream:
                stream.seek(self._offset)
                while True:
                    line_start = stream.tell()
                    line = stream.readline()
                    if not line:
                        break
                    if not line.endswith("\n"):
                        self._partial_line_start = line_start
                        break
                    self._partial_line_start = None
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        self._consume_event(
                            event,
                            fallback_time=file_stat.st_mtime,
                        )
                self._offset = (
                    self._partial_line_start
                    if self._partial_line_start is not None
                    else stream.tell()
                )
            file_stat = path.stat()
        except OSError:
            return None

        if self.last_event_at == 0:
            self.last_event_at = file_stat.st_mtime
        if self.last_progress_at == 0:
            self.last_progress_at = self.last_event_at
            self.tokens_at_last_progress = self.total_tokens
        return ThreadTelemetry(
            thread_id=self.thread_id,
            rollout_path=path,
            rollout_bytes=file_stat.st_size,
            total_tokens=self.total_tokens,
            context_tokens=self.context_tokens,
            context_window=self.context_window,
            compaction_count=self.compaction_count,
            tokens_at_last_progress=self.tokens_at_last_progress,
            last_event_at=self.last_event_at,
            last_progress_at=self.last_progress_at,
            verified_event_count=self.verified_event_count,
            progress_event_count=self.progress_event_count,
            latest_failure=self.latest_failure,
        )


def find_latest_task_failure(
    *, thread_id: str, sessions_root: Path = DEFAULT_SESSIONS_ROOT
) -> TaskFailure | None:
    path = find_thread_rollout_path(
        thread_id=thread_id,
        sessions_root=sessions_root,
    )
    if path is None:
        return None
    latest: TaskFailure | None = None
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = event.get("payload", {})
                error = payload.get("error")
                if (
                    event.get("type") != "event_msg"
                    or payload.get("type") != "task_complete"
                    or not isinstance(error, dict)
                    or not isinstance(error.get("message"), str)
                ):
                    continue
                incident_id = payload.get("turn_id") or event.get("timestamp")
                if not isinstance(incident_id, str) or not incident_id:
                    continue
                error_info = error.get("codex_error_info")
                latest = TaskFailure(
                    incident_id=incident_id,
                    message=error["message"],
                    codex_error_info=(
                        error_info if isinstance(error_info, str) else None
                    ),
                )
    except OSError:
        return None
    return latest


def find_latest_goal_objective(
    *, thread_id: str, sessions_root: Path = DEFAULT_SESSIONS_ROOT
) -> str | None:
    path = find_thread_rollout_path(
        thread_id=thread_id,
        sessions_root=sessions_root,
    )
    if path is None:
        return None
    latest: str | None = None
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = event.get("payload", {})
                if (
                    event.get("type") != "response_item"
                    or payload.get("type") != "message"
                    or payload.get("role") != "user"
                ):
                    continue
                for item in payload.get("content", []):
                    if not isinstance(item, dict) or item.get("type") != "input_text":
                        continue
                    text = item.get("text")
                    if not isinstance(text, str) or "<objective>" not in text:
                        continue
                    try:
                        context = ET.fromstring(text.strip())
                    except ET.ParseError:
                        continue
                    if (
                        context.tag != "codex_internal_context"
                        or context.get("source") != "goal"
                    ):
                        continue
                    objective = context.findtext("objective")
                    if objective and objective.strip():
                        latest = objective.strip()
    except OSError:
        return None
    return latest


def compaction_event_exists_after(path: Path, *, offset: int) -> bool:
    try:
        with path.open("r", encoding="utf-8") as stream:
            stream.seek(offset)
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "compacted":
                    return True
                if event.get("payload", {}).get("type") == "context_compacted":
                    return True
    except OSError:
        return False
    return False
