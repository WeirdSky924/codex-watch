"""Persist Codex thread bindings independently of tmux lifetime."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
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
    recovery_count: int = 0
    successful_compactions: int = 0
    verification_pending: bool = False
    verification_baseline: int = 0


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
            recovery_count=_nonnegative_int(payload.get("recovery_count", 0)),
            successful_compactions=_nonnegative_int(
                payload.get("successful_compactions", 0)
            ),
            verification_pending=payload.get("verification_pending") is True,
            verification_baseline=_nonnegative_int(
                payload.get("verification_baseline", 0)
            ),
        )
    except (OSError, KeyError, TypeError, json.JSONDecodeError, ValueError):
        return None


def _nonnegative_int(value: object) -> int:
    return max(0, value) if type(value) is int else 0


def _atomic_json_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
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


def save_session_binding(
    *,
    session: str,
    thread_id: str,
    cwd: Path,
    state_root: Path | None = None,
    recovery_count: int | None = None,
    successful_compactions: int | None = None,
    verification_pending: bool | None = None,
    verification_baseline: int | None = None,
) -> SessionBinding:
    normalized_thread_id = validate_thread_id(thread_id)
    resolved_cwd = cwd.expanduser().resolve()
    path = _binding_path(session, state_root=state_root)
    previous = load_session_binding(session, state_root=state_root)
    same_thread = previous is not None and previous.thread_id == normalized_thread_id
    previous_recovery_count = (
        previous.recovery_count if same_thread and previous is not None else 0
    )
    previous_compactions = (
        previous.successful_compactions
        if same_thread and previous is not None
        else 0
    )
    previous_verification_pending = (
        previous.verification_pending
        if same_thread and previous is not None
        else False
    )
    previous_verification_baseline = (
        previous.verification_baseline
        if same_thread and previous is not None
        else 0
    )
    resolved_recovery_count = _nonnegative_int(
        recovery_count
        if recovery_count is not None
        else previous_recovery_count
    )
    resolved_compactions = _nonnegative_int(
        successful_compactions
        if successful_compactions is not None
        else previous_compactions
    )
    resolved_verification_pending = (
        verification_pending
        if verification_pending is not None
        else previous_verification_pending
    )
    resolved_verification_baseline = _nonnegative_int(
        verification_baseline
        if verification_baseline is not None
        else previous_verification_baseline
    )
    payload = {
        "session": session,
        "thread_id": normalized_thread_id,
        "cwd": str(resolved_cwd),
        "recovery_count": resolved_recovery_count,
        "successful_compactions": resolved_compactions,
        "verification_pending": resolved_verification_pending,
        "verification_baseline": resolved_verification_baseline,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_json_write(path, payload)
    return SessionBinding(
        session=session,
        thread_id=normalized_thread_id,
        cwd=resolved_cwd,
        recovery_count=resolved_recovery_count,
        successful_compactions=resolved_compactions,
        verification_pending=resolved_verification_pending,
        verification_baseline=resolved_verification_baseline,
    )


def save_binding_runtime_state(
    *,
    session: str,
    recovery_count: int,
    successful_compactions: int,
    verification_pending: bool | None = None,
    verification_baseline: int | None = None,
    state_root: Path | None = None,
) -> SessionBinding | None:
    binding = load_session_binding(session, state_root=state_root)
    if binding is None:
        return None
    return save_session_binding(
        session=session,
        thread_id=binding.thread_id,
        cwd=binding.cwd,
        state_root=state_root,
        recovery_count=recovery_count,
        successful_compactions=successful_compactions,
        verification_pending=verification_pending,
        verification_baseline=verification_baseline,
    )


def save_thread_handoff(
    *,
    session: str,
    thread_id: str,
    cwd: Path,
    reason: str,
    goal_objective: str | None,
    telemetry: Mapping[str, object],
    state_root: Path | None = None,
) -> Path:
    normalized_thread_id = validate_thread_id(thread_id)
    root = state_dir() if state_root is None else state_root
    digest = hashlib.sha256(session.encode("utf-8")).hexdigest()
    path = root / "handoffs" / f"{digest}.json"
    payload = {
        "schema_version": 1,
        "session": session,
        "old_thread_id": normalized_thread_id,
        "cwd": str(cwd.expanduser().resolve()),
        "reason": reason,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "goal_objective": (goal_objective or "")[:20_000],
        "telemetry": dict(telemetry),
        "recovery_order": [
            "latest_user_requirement",
            "current_worktree",
            "unique_ACTIVE_Plan_State",
            "canonical_project_records",
            "host_local_handoff",
        ],
    }
    _atomic_json_write(path, payload)
    return path
