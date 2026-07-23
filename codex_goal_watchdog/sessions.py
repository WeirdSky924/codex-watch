"""Resolve Codex rollout metadata to stable thread IDs."""

from __future__ import annotations

import json
import time
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
