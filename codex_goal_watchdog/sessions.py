"""Resolve Codex rollout metadata to stable thread IDs."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID


DEFAULT_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"


@dataclass(frozen=True)
class SessionRecord:
    path: Path
    thread_id: str
    cwd: Path
    started_at: datetime
    modified_at: float


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


def find_new_thread_id(
    *,
    cwd: Path,
    started_after: datetime,
    sessions_root: Path = DEFAULT_SESSIONS_ROOT,
) -> str | None:
    resolved_cwd = cwd.resolve()
    threshold = started_after.astimezone(timezone.utc)
    matches = [
        record
        for record in _session_records(sessions_root)
        if record.cwd == resolved_cwd and record.started_at >= threshold
    ]
    if not matches:
        return None
    return max(matches, key=lambda record: record.started_at).thread_id


def wait_for_new_thread_id(
    *,
    cwd: Path,
    started_after: datetime,
    sessions_root: Path = DEFAULT_SESSIONS_ROOT,
    timeout_seconds: float = 15,
) -> str | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        thread_id = find_new_thread_id(
            cwd=cwd,
            started_after=started_after,
            sessions_root=sessions_root,
        )
        if thread_id:
            return thread_id
        time.sleep(0.1)
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
