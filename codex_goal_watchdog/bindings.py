"""Persist Codex thread bindings independently of tmux lifetime."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .paths import state_dir
from .sessions import validate_thread_id


@dataclass(frozen=True)
class SessionBinding:
    session: str
    thread_id: str
    cwd: Path


def _binding_path(session: str, *, state_root: Path | None = None) -> Path:
    digest = hashlib.sha256(session.encode("utf-8")).hexdigest()
    root = state_dir() if state_root is None else state_root
    return root / "bindings" / f"{digest}.json"


def load_session_binding(
    session: str,
    *,
    state_root: Path | None = None,
) -> SessionBinding | None:
    path = _binding_path(session, state_root=state_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("session") != session:
            return None
        return SessionBinding(
            session=session,
            thread_id=validate_thread_id(payload["thread_id"]),
            cwd=Path(payload["cwd"]).expanduser().resolve(),
        )
    except (OSError, KeyError, TypeError, json.JSONDecodeError, ValueError):
        return None


def save_session_binding(
    *,
    session: str,
    thread_id: str,
    cwd: Path,
    state_root: Path | None = None,
) -> SessionBinding:
    normalized_thread_id = validate_thread_id(thread_id)
    resolved_cwd = cwd.expanduser().resolve()
    path = _binding_path(session, state_root=state_root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    payload = {
        "session": session,
        "thread_id": normalized_thread_id,
        "cwd": str(resolved_cwd),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with temporary_path.open("x", encoding="utf-8") as stream:
            os.chmod(temporary_path, 0o600)
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    finally:
        temporary_path.unlink(missing_ok=True)
    return SessionBinding(
        session=session,
        thread_id=normalized_thread_id,
        cwd=resolved_cwd,
    )
