"""Carry a Codex turn's execution profile into fatal recovery."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Protocol, TypeAlias

from .recovery import RecoveryConfig, classify_recovery_message

MAX_TURN_EXECUTION_PROFILES = 256
PRIMARY_MODEL_OPTION = "@codex_primary_model"
PRIMARY_EFFORT_OPTION = "@codex_primary_effort"


def _nonempty_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class ExecutionProfile:
    model: str | None = None
    reasoning_effort: str | None = None

    @property
    def complete(self) -> bool:
        return self.model is not None and self.reasoning_effort is not None


class TurnExecutionProfileIndex:
    """Keep recent turn profiles while incrementally reading a rollout."""

    def __init__(self, *, limit: int = MAX_TURN_EXECUTION_PROFILES) -> None:
        self._limit = max(1, limit)
        self._profiles: dict[str, ExecutionProfile] = {}

    def observe(self, event: Mapping[str, object]) -> None:
        if event.get("type") != "turn_context":
            return
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        turn_id = _nonempty_text(payload.get("turn_id"))
        if turn_id is None:
            return
        previous = self._profiles.get(turn_id, ExecutionProfile())
        profile = ExecutionProfile(
            model=_nonempty_text(payload.get("model")) or previous.model,
            reasoning_effort=(
                _nonempty_text(payload.get("effort")) or previous.reasoning_effort
            ),
        )
        if profile == ExecutionProfile():
            return
        self._profiles.pop(turn_id, None)
        self._profiles[turn_id] = profile
        while len(self._profiles) > self._limit:
            self._profiles.pop(next(iter(self._profiles)))

    def profile_for(self, turn_id: str) -> ExecutionProfile:
        return self._profiles.get(turn_id, ExecutionProfile())


class TaskFailureLike(Protocol):
    @property
    def incident_id(self) -> str: ...

    @property
    def message(self) -> str: ...

    @property
    def model(self) -> str | None: ...

    @property
    def reasoning_effort(self) -> str | None: ...


@dataclass(frozen=True)
class RecoveryIncident:
    incident_id: str
    reason: str
    model: str | None = None
    reasoning_effort: str | None = None

    @property
    def profile_complete(self) -> bool:
        return self.model is not None and self.reasoning_effort is not None


RecoveryIncidentLike: TypeAlias = RecoveryIncident | tuple[str, str]


def recovery_incident_from_failure(
    failure: TaskFailureLike,
) -> RecoveryIncident | None:
    reason = classify_recovery_message(failure.message)
    if reason is None:
        return None
    return RecoveryIncident(
        incident_id=failure.incident_id,
        reason=reason,
        model=failure.model,
        reasoning_effort=failure.reasoning_effort,
    )


def normalize_recovery_incident(
    incident: RecoveryIncidentLike | None,
) -> RecoveryIncident | None:
    if incident is None or isinstance(incident, RecoveryIncident):
        return incident
    if (
        isinstance(incident, tuple)
        and len(incident) == 2
        and all(isinstance(value, str) and value for value in incident)
    ):
        return RecoveryIncident(incident_id=incident[0], reason=incident[1])
    return None


def matching_recovery_incident(
    resolver: Callable[[str], RecoveryIncidentLike | None] | None,
    thread_id: str,
    reason: str,
) -> RecoveryIncident | None:
    if resolver is None:
        return None
    incident = normalize_recovery_incident(resolver(thread_id))
    return incident if incident is not None and incident.reason == reason else None


def recovery_incident_id(incident: RecoveryIncidentLike | None) -> str:
    normalized = normalize_recovery_incident(incident)
    return normalized.incident_id if normalized is not None else ""


def find_unhandled_recovery_incident(
    session: str,
    *,
    thread_id: str,
    screen_reason: Callable[[str], str | None],
    incident_resolver: Callable[[str], RecoveryIncidentLike | None],
    option_getter: Callable[[str, str, str], str],
    last_incident_option: str,
) -> RecoveryIncidentLike | None:
    reason = screen_reason(session)
    if reason is None:
        return None
    raw_incident = incident_resolver(thread_id)
    incident = normalize_recovery_incident(raw_incident)
    if incident is None or incident.reason != reason:
        return None
    if incident.incident_id == option_getter(session, last_incident_option, ""):
        return None
    return raw_incident


def resolve_and_claim_recovery_incident(
    *,
    resolver: Callable[[str], RecoveryIncidentLike | None] | None,
    thread_id: str,
    reason: str,
    handled_incident_ids: dict[str, None],
    claim: Callable[[str], bool] | None,
    save: Callable[[str], None] | None,
    record: Callable[..., None],
) -> tuple[RecoveryIncident | None, bool]:
    """Resolve one visible failure and report whether the caller should skip it."""
    if resolver is None:
        return None, False
    incident = matching_recovery_incident(resolver, thread_id, reason)
    if incident is None:
        record(
            key=f"unmatched:{reason}",
            first=(
                "[codex-goal-watchdog] ignored terminal error without "
                f"matching rollout event: {reason}"
            ),
            summary=(
                "[codex-goal-watchdog] unmatched terminal error aggregate: "
                f"{reason}"
            ),
        )
        return None, True
    incident_id = incident.incident_id
    if incident_id in handled_incident_ids:
        record(
            key=f"redrawn:{incident_id}",
            first=(
                "[codex-goal-watchdog] ignored redrawn fatal event: "
                f"{incident_id}"
            ),
            summary=(
                "[codex-goal-watchdog] fatal redraw aggregate: "
                f"{incident_id}"
            ),
        )
        return None, True
    handled_incident_ids[incident_id] = None
    if len(handled_incident_ids) > 256:
        handled_incident_ids.pop(next(iter(handled_incident_ids)))
    if claim is not None and not claim(incident_id):
        record(
            key=f"claimed:{incident_id}",
            first=(
                "[codex-goal-watchdog] ignored fatal incident claimed by "
                f"another recovery owner: {incident_id}"
            ),
            summary=(
                "[codex-goal-watchdog] claimed incident aggregate: "
                f"{incident_id}"
            ),
        )
        return None, True
    if claim is None and save is not None:
        save(incident_id)
    return incident, False


def apply_incident_execution_profile(
    config: RecoveryConfig,
    incident: RecoveryIncident,
    *,
    save: Callable[[str, str], None] | None = None,
    target: str | None = None,
) -> RecoveryConfig:
    """Override recovery defaults only when the failing profile is complete."""
    if not incident.profile_complete:
        return config
    assert incident.model is not None
    assert incident.reasoning_effort is not None
    saver = save
    if saver is None and target is not None:
        saver = lambda model, effort: save_tmux_execution_profile(
            target, model, effort
        )
    if saver is not None:
        saver(incident.model, incident.reasoning_effort)
    return replace(
        config,
        primary_model=incident.model,
        primary_reasoning_effort=incident.reasoning_effort,
    )


def save_tmux_execution_profile(
    target: str,
    model: str,
    reasoning_effort: str,
    *,
    runner=subprocess.run,
) -> None:
    if not model or not reasoning_effort:
        raise ValueError("a complete Codex execution profile is required")
    for option, value in (
        (PRIMARY_MODEL_OPTION, model),
        (PRIMARY_EFFORT_OPTION, reasoning_effort),
    ):
        runner(
            ["tmux", "set-option", "-t", target, option, value],
            check=True,
        )
