import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_goal_watchdog.execution_profile import (
    RecoveryIncident,
    save_tmux_execution_profile,
)
from codex_goal_watchdog.guardian import _recover_visible_incident
from codex_goal_watchdog.monitor import run_monitor
from codex_goal_watchdog.recovery import RecoveryConfig
from codex_goal_watchdog.sessions import (
    ThreadTelemetryTracker,
    find_latest_task_failure,
    find_latest_task_failure_after,
)

THREAD_ID = "550e8400-e29b-41d4-a716-446655440000"


def _turn_context(turn_id: str, *, model: str, effort: str) -> dict:
    return {
        "timestamp": "2026-09-09T05:06:42Z",
        "type": "turn_context",
        "payload": {
            "turn_id": turn_id,
            "model": model,
            "effort": effort,
        },
    }


def _task_failure(turn_id: str, message: str) -> dict:
    return {
        "timestamp": "2026-09-09T05:11:42Z",
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "turn_id": turn_id,
            "error": {
                "message": message,
                "codex_error_info": "other",
            },
        },
    }


def _write_rollout(root: Path, *events: dict) -> Path:
    path = root / f"rollout-{THREAD_ID}.jsonl"
    session_meta = {
        "timestamp": "2026-09-09T05:00:00Z",
        "type": "session_meta",
        "payload": {
            "id": THREAD_ID,
            "cwd": "/workspace/project",
            "source": "cli",
            "timestamp": "2026-09-09T05:00:00Z",
        },
    }
    with path.open("w", encoding="utf-8") as stream:
        for event in (session_meta, *events):
            stream.write(json.dumps(event) + "\n")
    return path


def _shell_commands(steps) -> list[str]:
    return [step.value for step in steps if step.kind == "shell_command"]


class RolloutExecutionProfileTests(unittest.TestCase):
    def test_failure_resolvers_correlate_turn_context_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            turn_id = "turn-astra-503"
            _write_rollout(
                root,
                _turn_context(turn_id, model="gpt-6-astra", effort="xhigh"),
                _task_failure(
                    turn_id,
                    "unexpected status 503 Service Unavailable",
                ),
            )

            scanned = find_latest_task_failure(
                thread_id=THREAD_ID,
                sessions_root=root,
            )
            telemetry = ThreadTelemetryTracker(
                thread_id=THREAD_ID,
                sessions_root=root,
            ).snapshot()

        self.assertIsNotNone(scanned)
        assert scanned is not None
        self.assertEqual("gpt-6-astra", scanned.model)
        self.assertEqual("xhigh", scanned.reasoning_effort)
        self.assertIsNotNone(telemetry)
        assert telemetry is not None
        self.assertIsNotNone(telemetry.latest_failure)
        assert telemetry.latest_failure is not None
        self.assertEqual("gpt-6-astra", telemetry.latest_failure.model)
        self.assertEqual("xhigh", telemetry.latest_failure.reasoning_effort)

    def test_failure_after_offset_correlates_new_turn_context_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = _write_rollout(root)
            offset = path.stat().st_size
            turn_id = "turn-compact-503"
            with path.open("a", encoding="utf-8") as stream:
                for event in (
                    _turn_context(turn_id, model="gpt-5.6-luna", effort="xhigh"),
                    _task_failure(
                        turn_id,
                        "unexpected status 503 Service Unavailable",
                    ),
                ):
                    stream.write(json.dumps(event) + "\n")

            failure = find_latest_task_failure_after(path, offset=offset)

        self.assertIsNotNone(failure)
        assert failure is not None
        self.assertEqual("gpt-5.6-luna", failure.model)
        self.assertEqual("xhigh", failure.reasoning_effort)


class FatalRecoveryExecutionProfileTests(unittest.TestCase):
    def test_monitor_restarts_503_with_failing_turn_profile(self):
        calls = []
        saved_profiles = []

        run_monitor(
            lines=[
                "Pursuing goal (4m)\n",
                "■ unexpected status 503 Service Unavailable: upstream failed\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(
                thread_id=THREAD_ID,
                primary_model="gpt-5.6-sol",
                primary_reasoning_effort="max",
                cooldown_seconds=0,
            ),
            resolve_recovery_incident=lambda _thread_id: RecoveryIncident(
                "turn-astra-503",
                "retryable_http_503",
                model="gpt-6-astra",
                reasoning_effort="xhigh",
            ),
            claim_recovery_incident_id=lambda _incident_id: True,
            save_execution_profile=lambda model, effort: saved_profiles.append(
                (model, effort)
            ),
            execute=lambda target, steps: calls.append((target, steps)),
            now=iter([100.0, 101.0]).__next__,
            log=lambda _message: None,
        )

        commands = _shell_commands(calls[0][1])
        self.assertEqual([("gpt-6-astra", "xhigh")], saved_profiles)
        self.assertEqual(1, len(commands))
        self.assertIn("-m gpt-6-astra", commands[0])
        self.assertIn('model_reasoning_effort="xhigh"', commands[0])
        self.assertNotIn("gpt-5.6-sol", commands[0])

    def test_monitor_compacts_then_returns_to_failing_turn_profile(self):
        calls = []

        run_monitor(
            lines=[
                "Pursuing goal (4m)\n",
                "■ stream disconnected before completion: codex upstream stalled: "
                "no real data for 5m0s, connection recycled\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(
                thread_id=THREAD_ID,
                primary_model="gpt-5.6-sol",
                primary_reasoning_effort="max",
                compact_model="gpt-5.6-luna",
                compact_reasoning_effort="xhigh",
                cooldown_seconds=0,
            ),
            resolve_recovery_incident=lambda _thread_id: RecoveryIncident(
                "turn-astra-stall",
                "codex_upstream_stalled",
                model="gpt-6-astra",
                reasoning_effort="high",
            ),
            claim_recovery_incident_id=lambda _incident_id: True,
            save_execution_profile=lambda _model, _effort: None,
            execute=lambda target, steps: calls.append((target, steps)),
            now=iter([100.0, 101.0]).__next__,
            log=lambda _message: None,
        )

        commands = _shell_commands(calls[0][1])
        self.assertEqual(2, len(commands))
        self.assertIn("-m gpt-5.6-luna", commands[0])
        self.assertIn('model_reasoning_effort="xhigh"', commands[0])
        self.assertIn("-m gpt-6-astra", commands[1])
        self.assertIn('model_reasoning_effort="high"', commands[1])

    def test_access_denied_rotation_uses_failing_turn_profile(self):
        calls = []

        run_monitor(
            lines=[
                "Pursuing goal (4m)\n",
                "■ unexpected status 502 Bad Gateway: Upstream access denied\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(
                thread_id=THREAD_ID,
                primary_model="gpt-5.6-sol",
                primary_reasoning_effort="max",
                cooldown_seconds=0,
            ),
            resolve_recovery_incident=lambda _thread_id: RecoveryIncident(
                "turn-astra-denied",
                "upstream_access_denied",
                model="gpt-6-astra",
                reasoning_effort="xhigh",
            ),
            claim_recovery_incident_id=lambda _incident_id: True,
            save_execution_profile=lambda _model, _effort: None,
            mark_thread_rotation=lambda _count, _reason, _thread_id: None,
            execute=lambda target, steps: calls.append((target, steps)),
            now=iter([100.0, 101.0]).__next__,
            log=lambda _message: None,
        )

        command = _shell_commands(calls[0][1])[0]
        self.assertIn("-m gpt-6-astra", command)
        self.assertIn('model_reasoning_effort="xhigh"', command)
        self.assertNotIn(f"resume {THREAD_ID}", command)

    def test_monitor_keeps_config_fallback_for_legacy_tuple_incident(self):
        calls = []
        saved_profiles = []

        run_monitor(
            lines=[
                "Pursuing goal (4m)\n",
                "■ unexpected status 503 Service Unavailable: upstream failed\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(
                thread_id=THREAD_ID,
                primary_model="gpt-5.6-sol",
                primary_reasoning_effort="max",
                cooldown_seconds=0,
            ),
            resolve_recovery_incident=lambda _thread_id: (
                "turn-legacy-503",
                "retryable_http_503",
            ),
            claim_recovery_incident_id=lambda _incident_id: True,
            save_execution_profile=lambda model, effort: saved_profiles.append(
                (model, effort)
            ),
            execute=lambda target, steps: calls.append((target, steps)),
            now=iter([100.0, 101.0]).__next__,
            log=lambda _message: None,
        )

        command = _shell_commands(calls[0][1])[0]
        self.assertEqual([], saved_profiles)
        self.assertIn("-m gpt-5.6-sol", command)
        self.assertIn('model_reasoning_effort="max"', command)

    def test_complete_profile_is_persisted_to_tmux_options(self):
        commands = []

        save_tmux_execution_profile(
            "codex-goal",
            "gpt-6-astra",
            "xhigh",
            runner=lambda command, **_kwargs: commands.append(command),
        )

        self.assertEqual(
            [
                [
                    "tmux",
                    "set-option",
                    "-t",
                    "codex-goal",
                    "@codex_primary_model",
                    "gpt-6-astra",
                ],
                [
                    "tmux",
                    "set-option",
                    "-t",
                    "codex-goal",
                    "@codex_primary_effort",
                    "xhigh",
                ],
            ],
            commands,
        )

    @patch("codex_goal_watchdog.guardian._set_recovery_phase")
    @patch("codex_goal_watchdog.guardian._mark_verification_pending")
    @patch("codex_goal_watchdog.guardian._next_recovery_attempt", return_value=1)
    @patch(
        "codex_goal_watchdog.guardian.recovery_goal_state_on_screen",
        return_value="pursuing",
    )
    @patch(
        "codex_goal_watchdog.guardian._claim_tmux_recovery_incident_id",
        return_value=True,
    )
    def test_guardian_restarts_with_failing_turn_profile(
        self,
        _claim_mock,
        _goal_state_mock,
        _attempt_mock,
        _verification_mock,
        _phase_mock,
    ):
        calls = []
        saved_profiles = []

        recovered = _recover_visible_incident(
            "codex-goal",
            RecoveryConfig(
                thread_id=THREAD_ID,
                primary_model="gpt-5.6-sol",
                primary_reasoning_effort="max",
                cooldown_seconds=0,
            ),
            RecoveryIncident(
                "turn-astra-502",
                "retryable_http_502",
                model="gpt-6-astra",
                reasoning_effort="xhigh",
            ),
            log_path=Path("/tmp/codex-watch-profile-test.log"),
            execute_steps=lambda target, steps: calls.append((target, steps)),
            save_execution_profile=lambda model, effort: saved_profiles.append(
                (model, effort)
            ),
        )

        command = _shell_commands(calls[0][1])[0]
        self.assertTrue(recovered)
        self.assertEqual([("gpt-6-astra", "xhigh")], saved_profiles)
        self.assertIn("-m gpt-6-astra", command)
        self.assertIn('model_reasoning_effort="xhigh"', command)


if __name__ == "__main__":
    unittest.main()
