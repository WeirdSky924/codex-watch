import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_goal_watchdog.monitor import (
    MONITOR_TICK,
    _claim_tmux_recovery_incident_id,
    _save_tmux_thread_id,
    iter_decoded_chunks,
    run_monitor,
)
from codex_goal_watchdog.recovery import RecoveryConfig
from codex_goal_watchdog.sessions import ThreadTelemetry
from codex_goal_watchdog.bindings import SessionBinding


THREAD_ID = "550e8400-e29b-41d4-a716-446655440000"


class MonitorTests(unittest.TestCase):
    def test_recovery_incident_claim_persists_only_once(self):
        current_incident = {"value": "old-turn"}

        def save_incident(_target, incident_id):
            current_incident["value"] = incident_id

        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "incident.lock"
            first_claim = _claim_tmux_recovery_incident_id(
                "codex-goal",
                "new-turn",
                option_getter=lambda target: current_incident["value"],
                option_saver=save_incident,
                lock_path=lock_path,
            )
            second_claim = _claim_tmux_recovery_incident_id(
                "codex-goal",
                "new-turn",
                option_getter=lambda target: current_incident["value"],
                option_saver=save_incident,
                lock_path=lock_path,
            )

        self.assertTrue(first_claim)
        self.assertFalse(second_claim)
        self.assertEqual("new-turn", current_incident["value"])

    def test_iter_decoded_chunks_yields_tui_output_without_newline(self):
        stream = io.BytesIO(
            b"stream disconnected: codex upstream stalled: "
            b"no real data for 5m0s, connection recycled"
        )

        chunks = list(iter_decoded_chunks(stream, chunk_size=16))

        self.assertEqual(
            "stream disconnected: codex upstream stalled: "
            "no real data for 5m0s, connection recycled",
            "".join(chunks),
        )

    def test_run_monitor_executes_recovery_on_matching_line(self):
        calls = []

        run_monitor(
            lines=[
                "Pursuing goal (4m)\n",
                "ordinary output\n",
                "■ stream disconnected before completion: codex upstream stalled: "
                "no real data for 5m0s, connection recycled\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID, cooldown_seconds=0),
            now=lambda: 100.0,
            execute=lambda target, steps: calls.append((target, steps)),
            resume_goal=lambda target: None,
            log=lambda message: None,
        )

        self.assertEqual(1, len(calls))
        self.assertEqual("codex-goal", calls[0][0])
        self.assertEqual("key", calls[0][1][0].kind)

    def test_run_monitor_does_not_submit_goal_resume_when_codex_is_missing(self):
        resume_calls = []
        messages = []

        run_monitor(
            lines=["Goal paused (/goal resume)\n"],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID),
            codex_running=lambda _target: False,
            resume_goal=lambda target: resume_calls.append(target),
            now=lambda: 100.0,
            log=messages.append,
        )

        self.assertEqual([], resume_calls)
        self.assertTrue(
            any("Codex process is not running" in message for message in messages)
        )

    def test_run_monitor_does_not_submit_update_when_codex_is_missing(self):
        update_calls = []
        messages = []

        run_monitor(
            lines=[
                "Update available! 0.144.6 -> 0.145.0\n",
                "1. Update now (runs npm install)\n",
                "2. Skip\n3. Skip until next version\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID),
            codex_running=lambda _target: False,
            update_codex=lambda target, version: update_calls.append(
                (target, version)
            ),
            now=iter([100.0, 101.0, 102.0]).__next__,
            log=messages.append,
        )

        self.assertEqual([], update_calls)
        self.assertTrue(
            any("skipped Codex update" in message for message in messages)
        )

    def test_run_monitor_suppresses_recovery_after_goal_achieved(self):
        calls = []
        persisted_counts = []

        run_monitor(
            lines=[
                "Pursuing goal (4m)\n",
                "Goal achieved\n",
                "■ unexpected status 503 Service Unavailable: upstream failed\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID, cooldown_seconds=0),
            now=lambda: 100.0,
            execute=lambda target, steps: calls.append((target, steps)),
            save_recovery_count=persisted_counts.append,
            log=lambda message: None,
        )

        self.assertEqual([], calls)
        self.assertEqual([], persisted_counts)

    @patch("codex_goal_watchdog.monitor.load_session_binding", return_value=None)
    @patch("codex_goal_watchdog.monitor._save_recovery_phase")
    def test_goal_achieved_clears_pending_recovery_state(
        self,
        save_recovery_phase_mock,
        _load_binding_mock,
    ):
        persisted_counts = []
        verification_states = []

        run_monitor(
            lines=["Goal achieved\n"],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID),
            initial_recovery_count=2,
            initial_verification_pending=True,
            initial_verification_baseline=32,
            save_recovery_count=persisted_counts.append,
            save_verification_state=lambda pending, baseline: (
                verification_states.append((pending, baseline))
            ),
            log=lambda message: None,
        )

        self.assertEqual([0], persisted_counts)
        self.assertEqual([(False, 0)], verification_states)
        save_recovery_phase_mock.assert_called_once_with(
            "codex-goal",
            "idle",
            reason="",
            not_before=0.0,
        )

    @patch("codex_goal_watchdog.monitor.load_session_binding", return_value=None)
    @patch("codex_goal_watchdog.monitor._save_recovery_phase")
    def test_monitor_startup_clears_pending_state_for_achieved_goal(
        self,
        _save_recovery_phase_mock,
        _load_binding_mock,
    ):
        persisted_counts = []
        verification_states = []

        run_monitor(
            lines=[],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID),
            initial_goal_state="achieved",
            initial_recovery_count=2,
            initial_verification_pending=True,
            initial_verification_baseline=32,
            save_recovery_count=persisted_counts.append,
            save_verification_state=lambda pending, baseline: (
                verification_states.append((pending, baseline))
            ),
            log=lambda message: None,
        )

        self.assertEqual([0], persisted_counts)
        self.assertEqual([(False, 0)], verification_states)

    def test_run_monitor_suppresses_recovery_without_goal_state(self):
        calls = []

        run_monitor(
            lines=[
                "■ unexpected status 503 Service Unavailable: upstream failed\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID, cooldown_seconds=0),
            now=lambda: 100.0,
            execute=lambda target, steps: calls.append((target, steps)),
            resume_goal=lambda target: None,
            log=lambda message: None,
        )

        self.assertEqual([], calls)

    def test_run_monitor_recovers_when_goal_blocked(self):
        calls = []

        run_monitor(
            lines=[
                "Goal blocked (/goal resume)\n",
                "■ unexpected status 503 Service Unavailable: upstream failed\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID, cooldown_seconds=0),
            now=lambda: 100.0,
            execute=lambda target, steps: calls.append((target, steps)),
            resume_goal=lambda target: None,
            log=lambda message: None,
        )

        self.assertEqual(1, len(calls))
        self.assertEqual("leave_goal_paused", calls[0][1][-1].kind)

    def test_run_monitor_leaves_blocked_goal_for_manual_review(self):
        recoveries = []
        resumed_targets = []
        messages = []

        run_monitor(
            lines=[
                "Goal blocked (/goal resume)\n",
                "Resume paused goal?\n1. Resume goal\n2. Leave paused\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID),
            now=iter([100.0, 101.0]).__next__,
            execute=lambda target, steps: recoveries.append((target, steps)),
            resume_goal=resumed_targets.append,
            log=messages.append,
        )

        self.assertEqual([], recoveries)
        self.assertEqual([], resumed_targets)
        self.assertTrue(any("manual" in message for message in messages))

    def test_run_monitor_leaves_stalled_goal_without_fatal_error(self):
        recoveries = []
        resumed_targets = []

        run_monitor(
            lines=[
                "Goal stalled (/goal resume)\n",
                "Resume paused goal?\n1. Resume goal\n2. Leave paused\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID),
            now=iter([100.0, 101.0]).__next__,
            execute=lambda target, steps: recoveries.append((target, steps)),
            resume_goal=resumed_targets.append,
            log=lambda message: None,
        )

        self.assertEqual([], recoveries)
        self.assertEqual([], resumed_targets)

    def test_run_monitor_recovers_new_fatal_error_while_goal_stalled(self):
        calls = []

        run_monitor(
            lines=[
                "Goal stalled (/goal resume)\n",
                "■ unexpected status 503 Service Unavailable: upstream failed\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID, cooldown_seconds=0),
            now=iter([100.0, 101.0]).__next__,
            execute=lambda target, steps: calls.append((target, steps)),
            resolve_recovery_incident=lambda thread_id: (
                "turn-stalled-503",
                "retryable_http_503",
            ),
            log=lambda message: None,
        )

        self.assertEqual(1, len(calls))
        self.assertEqual("resume_stalled_goal_or_prompt", calls[0][1][-1].kind)

    def test_pending_verification_suppresses_health_rotation_until_progress(self):
        calls = []
        telemetry = ThreadTelemetry(
            thread_id=THREAD_ID,
            rollout_path=Path("/tmp/rollout.jsonl"),
            rollout_bytes=100,
            total_tokens=100,
            context_tokens=100,
            context_window=1_000,
            compaction_count=0,
            tokens_at_last_progress=100,
            last_event_at=0.0,
            last_progress_at=0.0,
        )

        run_monitor(
            lines=["Pursuing goal (4m)\n", MONITOR_TICK],
            target="codex-goal",
            config=RecoveryConfig(
                thread_id=THREAD_ID,
                thread_no_event_seconds=1,
            ),
            initial_verification_pending=True,
            resolve_thread_telemetry=lambda _thread_id: telemetry,
            now=iter([100.0, 200.0]).__next__,
            execute=lambda target, steps: calls.append((target, steps)),
            log=lambda _message: None,
        )

        self.assertEqual([], calls)

    def test_run_monitor_keeps_manual_stall_for_redrawn_fatal_incident(self):
        calls = []

        run_monitor(
            lines=[
                "Goal stalled (/goal resume)\n",
                "■ unexpected status 503 Service Unavailable: upstream failed\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID, cooldown_seconds=0),
            now=iter([100.0, 101.0]).__next__,
            execute=lambda target, steps: calls.append((target, steps)),
            resolve_recovery_incident=lambda thread_id: (
                "turn-stalled-503",
                "retryable_http_503",
            ),
            initial_recovery_incident_id="turn-stalled-503",
            log=lambda message: None,
        )

        self.assertEqual([], calls)

    def test_run_monitor_ignores_redraw_of_same_rollout_failure(self):
        calls = []
        persisted_incidents = []

        run_monitor(
            lines=[
                "Pursuing goal (4m)\n",
                "■ unexpected status 503 Service Unavailable: upstream failed\n",
                "Goal paused (/goal resume)\n",
                "Pursuing goal (5m)\n",
                "■ unexpected status 503 Service Unavailable: upstream failed\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID, cooldown_seconds=0),
            now=iter(float(index) for index in range(5)).__next__,
            execute=lambda target, steps: calls.append((target, steps)),
            resume_goal=lambda target: None,
            resolve_recovery_incident=lambda thread_id: (
                "turn-503",
                "retryable_http_503",
            ),
            save_recovery_incident_id=persisted_incidents.append,
            log=lambda message: None,
        )

        self.assertEqual(1, len(calls))
        self.assertEqual(["turn-503"], persisted_incidents)

    def test_run_monitor_skips_incident_claimed_by_guardian(self):
        calls = []
        claims = []

        run_monitor(
            lines=[
                "Pursuing goal (4m)\n",
                "■ unexpected status 503 Service Unavailable: upstream failed\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID, cooldown_seconds=0),
            now=iter([100.0, 101.0]).__next__,
            execute=lambda target, steps: calls.append((target, steps)),
            resolve_recovery_incident=lambda thread_id: (
                "turn-claimed-by-guardian",
                "retryable_http_503",
            ),
            claim_recovery_incident_id=lambda incident_id: (
                claims.append(incident_id) or False
            ),
            log=lambda message: None,
        )

        self.assertEqual(["turn-claimed-by-guardian"], claims)
        self.assertEqual([], calls)

    def test_run_monitor_recovers_same_error_for_a_new_turn(self):
        calls = []
        incidents = iter(
            [
                ("turn-503-a", "retryable_http_503"),
                ("turn-503-b", "retryable_http_503"),
            ]
        )

        run_monitor(
            lines=[
                "Pursuing goal (4m)\n",
                "■ unexpected status 503 Service Unavailable: upstream failed\n",
                "Pursuing goal (5m)\n",
                "■ unexpected status 503 Service Unavailable: upstream failed\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID, cooldown_seconds=0),
            now=iter(float(index) for index in range(4)).__next__,
            execute=lambda target, steps: calls.append((target, steps)),
            resume_goal=lambda target: None,
            resolve_recovery_incident=lambda thread_id: next(incidents),
            log=lambda message: None,
        )

        self.assertEqual(2, len(calls))

    def test_run_monitor_does_not_execute_for_non_matching_lines(self):
        calls = []

        run_monitor(
            lines=["ordinary output\n", "Reconnecting... 1/5\n"],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID, cooldown_seconds=0),
            now=lambda: 100.0,
            execute=lambda target, steps: calls.append((target, steps)),
            log=lambda message: None,
        )

        self.assertEqual([], calls)

    def test_run_monitor_resumes_delayed_paused_goal_picker(self):
        resumed_targets = []

        run_monitor(
            lines=[
                "Loading conversation history...\n",
                "Resume paused goal?\n1. Resume goal\n2. Leave paused\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID),
            now=iter([100.0, 101.0]).__next__,
            resume_goal=resumed_targets.append,
            log=lambda message: None,
        )

        self.assertEqual(["codex-goal"], resumed_targets)

    def test_run_monitor_ignores_plain_text_picker_mention(self):
        resumed_targets = []

        run_monitor(
            lines=["The text Resume paused goal? may appear in documentation.\n"],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID),
            now=lambda: 100.0,
            resume_goal=resumed_targets.append,
            log=lambda message: None,
        )

        self.assertEqual([], resumed_targets)

    def test_run_monitor_executes_real_update_for_complete_picker(self):
        updates = []

        run_monitor(
            lines=[
                "Update available! 0.144.6 -> 0.145.0\n",
                "1. Update now (runs `npm install -g @openai/codex`)\n",
                "2. Skip\n3. Skip until next version\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID),
            now=iter([100.0, 101.0, 102.0]).__next__,
            update_codex=lambda target, version: updates.append((target, version)),
            log=lambda message: None,
        )

        self.assertEqual([("codex-goal", "0.145.0")], updates)

    def test_run_monitor_detects_wrapped_ansi_stall_output(self):
        calls = []

        run_monitor(
            lines=[
                "Pursuing goal (4m)\n",
                "\x1b[31m■ stream disconnected: codex upstream stalled:\x1b[0m\n",
                "no real data for 5m0s,\n",
                "connection recycled\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID, cooldown_seconds=0),
            now=lambda: 100.0,
            execute=lambda target, steps: calls.append((target, steps)),
            log=lambda message: None,
        )

        self.assertEqual(1, len(calls))

    def test_run_monitor_leaves_context_window_exhaustion_to_codex(self):
        calls = []

        run_monitor(
            lines=[
                "Pursuing goal (4m)\n",
                "■ Codex ran out of room in the model's context window. "
                "Start a new thread or clear earlier history before retrying.\n"
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID, cooldown_seconds=0),
            now=lambda: 100.0,
            execute=lambda target, steps: calls.append((target, steps)),
            log=lambda message: None,
        )

        self.assertEqual([], calls)

    def test_run_monitor_recovers_model_capacity_warning_without_compaction(self):
        calls = []

        run_monitor(
            lines=[
                "Pursuing goal (4m)\n",
                "⚠ Selected model is at capacity. Please try a different model\n"
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID, cooldown_seconds=300),
            now=lambda: 100.0,
            execute=lambda target, steps: calls.append((target, steps)),
            log=lambda message: None,
        )

        self.assertEqual(1, len(calls))
        self.assertNotIn("/compact", [step.value for step in calls[0][1]])
        self.assertEqual("0", calls[0][1][4].value)

    def test_run_monitor_recovers_server_overload_without_compaction(self):
        calls = []

        run_monitor(
            lines=[
                "Pursuing goal (4m)\n",
                "■ stream disconnected before completion: Our servers are "
                "currently overloaded. Please try again later.\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID, cooldown_seconds=300),
            now=lambda: 100.0,
            execute=lambda target, steps: calls.append((target, steps)),
            log=lambda message: None,
        )

        self.assertEqual(1, len(calls))
        self.assertNotIn("/compact", [step.value for step in calls[0][1]])
        self.assertEqual("0", calls[0][1][4].value)

    def test_run_monitor_retries_payment_required_without_attempt_limit(self):
        calls = []

        run_monitor(
            lines=[
                "Pursuing goal (4m)\n",
                *[
                    "■ unexpected status 402 Payment Required: upstream request failed\n"
                    for _ in range(5)
                ],
            ],
            target="codex-goal",
            config=RecoveryConfig(
                thread_id=THREAD_ID,
                cooldown_seconds=300,
                max_recoveries=0,
            ),
            now=iter(float(index) for index in range(6)).__next__,
            execute=lambda target, steps: calls.append((target, steps)),
            log=lambda message: None,
        )

        self.assertEqual(5, len(calls))
        self.assertEqual("0", calls[0][1][4].value)
        for _, steps in calls[1:]:
            self.assertEqual("300", steps[4].value)

    def test_run_monitor_continues_persisted_recovery_count_after_reattach(self):
        calls = []
        persisted_counts = []

        run_monitor(
            lines=[
                "Pursuing goal (4m)\n",
                "■ unexpected status 402 Payment Required: upstream request failed\n"
            ],
            target="codex-goal",
            config=RecoveryConfig(
                thread_id=THREAD_ID,
                cooldown_seconds=300,
                max_recoveries=0,
            ),
            initial_recovery_count=1,
            save_recovery_count=persisted_counts.append,
            now=lambda: 100.0,
            execute=lambda target, steps: calls.append((target, steps)),
            log=lambda message: None,
        )

        self.assertEqual([2], persisted_counts)
        self.assertEqual("300", calls[0][1][4].value)

    def test_access_denied_rotation_rebinds_and_preserves_retry_cooldown(self):
        new_thread_id = "550e8400-e29b-41d4-a716-446655440001"
        calls = []
        rebound_ids = []
        persisted_counts = []
        marked_rotation_counts = []
        thread_ids = iter([THREAD_ID, THREAD_ID, new_thread_id, new_thread_id])
        incidents = iter(
            [
                ("turn-ban-old", "upstream_access_denied"),
                ("turn-ban-new", "upstream_access_denied"),
            ]
        )

        run_monitor(
            lines=[
                "Pursuing goal (4m)\n",
                "■ unexpected status 502 Bad Gateway: Upstream access denied\n",
                "Pursuing goal (1m)\n",
                "■ unexpected status 502 Bad Gateway: Upstream access denied\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(
                thread_id=THREAD_ID,
                cooldown_seconds=300,
                max_recoveries=0,
            ),
            initial_recovery_count=2,
            resolve_thread_id=lambda target: next(thread_ids),
            save_thread_id=rebound_ids.append,
            save_recovery_count=persisted_counts.append,
            resolve_recovery_incident=lambda thread_id: next(incidents),
            resolve_goal_objective=lambda thread_id: "Goal ID: FE-CREATOR-8",
            mark_thread_rotation=marked_rotation_counts.append,
            claim_recovery_incident_id=lambda incident_id: True,
            now=iter([100.0, 101.0, 102.0, 103.0]).__next__,
            execute=lambda target, steps: calls.append((target, steps)),
            log=lambda message: None,
        )

        self.assertEqual([new_thread_id], rebound_ids)
        self.assertEqual([3, 3, 4], persisted_counts)
        self.assertEqual([3, 4], marked_rotation_counts)
        self.assertEqual(2, len(calls))
        self.assertNotIn(THREAD_ID, calls[0][1][5].value)
        self.assertIn("Goal ID: FE-CREATOR-8", calls[0][1][-1].value)
        self.assertEqual("300", calls[1][1][4].value)

    def test_run_monitor_rebinds_after_clear_before_recovery(self):
        new_thread_id = "550e8400-e29b-41d4-a716-446655440001"
        calls = []
        rebound_ids = []
        persisted_counts = []

        run_monitor(
            lines=[
                "Pursuing goal (4m)\n",
                "■ unexpected status 503 Service Unavailable: "
                "service temporarily unavailable\n"
            ],
            target="codex-goal",
            config=RecoveryConfig(
                thread_id=THREAD_ID,
                cooldown_seconds=300,
                max_recoveries=0,
            ),
            initial_recovery_count=4,
            resolve_thread_id=lambda target: new_thread_id,
            save_thread_id=rebound_ids.append,
            save_recovery_count=persisted_counts.append,
            now=lambda: 100.0,
            execute=lambda target, steps: calls.append((target, steps)),
            log=lambda message: None,
        )

        self.assertEqual([new_thread_id], rebound_ids)
        self.assertEqual([1], persisted_counts)
        self.assertEqual("0", calls[0][1][4].value)
        self.assertTrue(
            any(new_thread_id in step.value for step in calls[0][1])
        )

    @patch("codex_goal_watchdog.monitor.save_session_binding")
    @patch(
        "codex_goal_watchdog.monitor._tmux_pane_identity",
        return_value=(123, Path("/workspace/project-a")),
    )
    @patch("codex_goal_watchdog.monitor.subprocess.run")
    def test_clear_rebind_persists_new_thread_for_next_tmux_start(
        self,
        _run_mock,
        _pane_identity_mock,
        save_binding_mock,
    ):
        new_thread_id = "550e8400-e29b-41d4-a716-446655440001"

        _save_tmux_thread_id("project-a", new_thread_id)

        save_binding_mock.assert_called_once_with(
            session="project-a",
            thread_id=new_thread_id,
            cwd=Path("/workspace/project-a"),
            verification_pending=None,
            verification_baseline=None,
        )

    @patch("codex_goal_watchdog.monitor._clear_pending_thread_rotation")
    @patch(
        "codex_goal_watchdog.monitor._pending_thread_rotation_count",
        return_value=3,
    )
    @patch("codex_goal_watchdog.monitor._save_tmux_recovery_count")
    @patch("codex_goal_watchdog.monitor.save_session_binding")
    @patch(
        "codex_goal_watchdog.monitor._tmux_pane_identity",
        return_value=(123, Path("/workspace/project-a")),
    )
    @patch("codex_goal_watchdog.monitor.subprocess.run")
    def test_rotation_rebind_preserves_pending_recovery_count(
        self,
        _run_mock,
        _pane_identity_mock,
        _save_binding_mock,
        save_count_mock,
        _pending_count_mock,
        clear_pending_mock,
    ):
        new_thread_id = "550e8400-e29b-41d4-a716-446655440001"

        _save_tmux_thread_id("project-a", new_thread_id)

        save_count_mock.assert_called_once_with("project-a", 3)
        clear_pending_mock.assert_called_once_with("project-a")

    @patch("codex_goal_watchdog.monitor.save_binding_runtime_state")
    @patch("codex_goal_watchdog.monitor.load_session_binding")
    @patch("codex_goal_watchdog.monitor.save_session_binding")
    @patch("codex_goal_watchdog.monitor._pending_thread_rotation_count", return_value=3)
    @patch("codex_goal_watchdog.monitor._save_tmux_recovery_count")
    @patch("codex_goal_watchdog.monitor._save_tmux_successful_compactions")
    @patch("codex_goal_watchdog.monitor._tmux_pane_identity", return_value=(123, Path("/workspace/project-a")))
    @patch("codex_goal_watchdog.monitor.subprocess.run")
    def test_thread_rebind_preserves_verification_pending_state(
        self,
        _run_mock,
        _pane_identity_mock,
        _save_compactions_mock,
        _save_count_mock,
        _pending_count_mock,
        save_binding_mock,
        load_binding_mock,
        save_runtime_mock,
    ):
        old_thread_id = "550e8400-e29b-41d4-a716-446655440000"
        new_thread_id = "550e8400-e29b-41d4-a716-446655440001"
        load_binding_mock.return_value = SessionBinding(
            session="project-a",
            thread_id=old_thread_id,
            cwd=Path("/workspace/project-a"),
            verification_pending=True,
            verification_baseline=17,
        )

        _save_tmux_thread_id("project-a", new_thread_id)

        self.assertEqual(
            {
                "session": "project-a",
                "thread_id": new_thread_id,
                "cwd": Path("/workspace/project-a"),
                "verification_pending": True,
                "verification_baseline": 17,
            },
            {
                "session": save_binding_mock.call_args.kwargs["session"],
                "thread_id": save_binding_mock.call_args.kwargs["thread_id"],
                "cwd": save_binding_mock.call_args.kwargs["cwd"],
                "verification_pending": save_binding_mock.call_args.kwargs.get(
                    "verification_pending"
                ),
                "verification_baseline": save_binding_mock.call_args.kwargs.get(
                    "verification_baseline"
                ),
            },
        )
        save_runtime_mock.assert_called_once_with(
            session="project-a",
            recovery_count=3,
            successful_compactions=0,
            verification_pending=True,
            verification_baseline=17,
        )

    def test_verified_recovery_resets_count_but_failed_verification_does_not(self):
        calls = []
        persisted_counts = []
        incidents = iter(
            [
                ("turn-503-a", "retryable_http_503"),
                ("turn-503-b", "retryable_http_503"),
            ]
        )
        verifications = iter([False, True])

        run_monitor(
            lines=[
                "Pursuing goal (4m)\n",
                "■ unexpected status 503 Service Unavailable: upstream failed\n",
                "Pursuing goal (5m)\n",
                "■ unexpected status 503 Service Unavailable: upstream failed\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID, cooldown_seconds=300),
            resolve_recovery_incident=lambda thread_id: next(incidents),
            claim_recovery_incident_id=lambda incident_id: True,
            verify_recovery=lambda target: next(verifications),
            save_recovery_count=persisted_counts.append,
            now=iter([100.0, 101.0, 102.0, 103.0]).__next__,
            execute=lambda target, steps: calls.append((target, steps)),
            log=lambda message: None,
        )

        self.assertEqual(2, len(calls))
        self.assertEqual([1, 2, 0], persisted_counts)
        self.assertEqual("300", calls[1][1][4].value)

    def test_duplicate_incident_redraws_log_once_then_aggregate(self):
        messages = []

        run_monitor(
            lines=[
                "Pursuing goal (4m)\n",
                *[
                    "■ unexpected status 503 Service Unavailable: upstream failed\n"
                    for _ in range(3)
                ],
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID),
            resolve_recovery_incident=lambda thread_id: (
                "turn-redrawn",
                "retryable_http_503",
            ),
            initial_recovery_incident_id="turn-redrawn",
            now=iter([100.0, 101.0, 102.0, 103.0]).__next__,
            log=messages.append,
        )

        first_logs = [
            message for message in messages if "ignored redrawn fatal event" in message
        ]
        summaries = [message for message in messages if "suppressed=2" in message]
        self.assertEqual(1, len(first_logs))
        self.assertEqual(1, len(summaries))

    def test_health_thresholds_are_telemetry_only(self):
        calls = []
        marked_counts = []
        handoffs = []
        telemetry = ThreadTelemetry(
            thread_id=THREAD_ID,
            rollout_path=Path("/tmp/rollout.jsonl"),
            rollout_bytes=2_000,
            total_tokens=10_000,
            context_tokens=400,
            context_window=1_000,
            compaction_count=0,
            tokens_at_last_progress=9_900,
            last_event_at=100.0,
            last_progress_at=100.0,
        )

        run_monitor(
            lines=["Pursuing goal (4m)\n", MONITOR_TICK],
            target="codex-goal",
            config=RecoveryConfig(
                thread_id=THREAD_ID,
                thread_max_rollout_bytes=1_000,
            ),
            resolve_thread_telemetry=lambda thread_id: telemetry,
            resolve_goal_objective=lambda thread_id: "Goal ID: FE-CREATOR-8",
            write_thread_handoff=lambda **kwargs: (
                handoffs.append(kwargs) or Path("/state/handoffs/latest.json")
            ),
            mark_thread_rotation=marked_counts.append,
            save_recovery_count=lambda count: None,
            now=iter([100.0, 101.0]).__next__,
            execute=lambda target, steps: calls.append((target, steps)),
            log=lambda message: None,
        )

        self.assertEqual([], calls)
        self.assertEqual([], marked_counts)
        self.assertEqual([], handoffs)

    def test_health_tick_ignores_context_usage_during_active_turn(self):
        calls = []
        telemetry = ThreadTelemetry(
            thread_id=THREAD_ID,
            rollout_path=Path("/tmp/rollout.jsonl"),
            rollout_bytes=100,
            total_tokens=10_000,
            context_tokens=900,
            context_window=1_000,
            compaction_count=0,
            tokens_at_last_progress=10_000,
            last_event_at=100.0,
            last_progress_at=100.0,
            turn_active=True,
        )

        run_monitor(
            lines=["Pursuing goal (4m)\n", MONITOR_TICK],
            target="codex-goal",
            config=RecoveryConfig(
                thread_id=THREAD_ID,
                thread_max_context_tokens=800,
            ),
            resolve_thread_telemetry=lambda thread_id: telemetry,
            now=iter([100.0, 101.0]).__next__,
            execute=lambda target, steps: calls.append((target, steps)),
            log=lambda message: None,
        )

        self.assertEqual([], calls)

    def test_health_tick_ignores_compaction_count(self):
        calls = []
        telemetry = ThreadTelemetry(
            thread_id=THREAD_ID,
            rollout_path=Path("/tmp/rollout.jsonl"),
            rollout_bytes=100,
            total_tokens=100,
            context_tokens=400,
            context_window=1_000,
            compaction_count=100,
            tokens_at_last_progress=100,
            last_event_at=100.0,
            last_progress_at=100.0,
        )

        run_monitor(
            lines=["Pursuing goal (4m)\n", MONITOR_TICK],
            target="codex-goal",
            config=RecoveryConfig(
                thread_id=THREAD_ID,
                thread_max_compactions=1,
            ),
            resolve_thread_telemetry=lambda thread_id: telemetry,
            now=iter([100.0, 101.0]).__next__,
            execute=lambda target, steps: calls.append((target, steps)),
            log=lambda message: None,
        )

        self.assertEqual([], calls)

    def test_repeated_command_detection_does_not_rotate_thread(self):
        calls = []
        handoffs = []
        telemetry = ThreadTelemetry(
            thread_id=THREAD_ID,
            rollout_path=Path("/tmp/rollout.jsonl"),
            rollout_bytes=100,
            total_tokens=100,
            context_tokens=400,
            context_window=1_000,
            compaction_count=0,
            tokens_at_last_progress=100,
            last_event_at=100.0,
            last_progress_at=100.0,
            repeated_command_count=3,
            repeated_command_signature="hash-command",
            turn_active=True,
        )

        run_monitor(
            lines=["Pursuing goal (4m)\n", MONITOR_TICK],
            target="codex-goal",
            config=RecoveryConfig(
                thread_id=THREAD_ID,
                thread_max_repeated_commands=3,
            ),
            resolve_thread_telemetry=lambda thread_id: telemetry,
            resolve_goal_objective=lambda thread_id: "Goal ID: FE-CREATOR-8",
            write_thread_handoff=lambda **kwargs: (
                handoffs.append(kwargs) or Path("/state/handoffs/latest.json")
            ),
            save_recovery_count=lambda count: None,
            now=iter([100.0, 101.0]).__next__,
            execute=lambda target, steps: calls.append((target, steps)),
            log=lambda message: None,
        )

        self.assertEqual([], calls)
        self.assertEqual([], handoffs)

    def test_monitor_tick_reconciles_current_thread_binding(self):
        new_thread_id = "550e8400-e29b-41d4-a716-446655440001"
        rebound_ids = []

        run_monitor(
            lines=[MONITOR_TICK],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID),
            resolve_thread_id=lambda _target: new_thread_id,
            save_thread_id=rebound_ids.append,
            now=lambda: 100.0,
            log=lambda _message: None,
        )

        self.assertEqual([new_thread_id], rebound_ids)

    def test_fatal_recovery_waits_for_persisted_cooldown(self):
        calls = []
        messages = []

        run_monitor(
            lines=[
                "Pursuing goal (4m)\n",
                "■ unexpected status 503 Service Unavailable: upstream failed\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID),
            initial_recovery_count=1,
            recovery_deferred=lambda: True,
            execute=lambda target, steps: calls.append((target, steps)),
            log=messages.append,
        )

        self.assertEqual(1, len(calls))
        self.assertTrue(
            any(step.kind == "sleep" and step.value == "300" for step in calls[0][1])
        )
        self.assertTrue(any("deferred during cooldown" in item for item in messages))

    def test_new_fatal_recovery_is_not_dropped_during_persisted_cooldown(self):
        calls = []
        messages = []

        run_monitor(
            lines=[
                "Pursuing goal (4m)\n",
                "■ unexpected status 503 Service Unavailable: upstream failed\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID, cooldown_seconds=300),
            initial_recovery_count=1,
            recovery_deferred=lambda: True,
            resolve_recovery_incident=lambda _thread_id: (
                "turn-new-503",
                "retryable_http_503",
            ),
            execute=lambda target, steps: calls.append((target, steps)),
            log=messages.append,
        )

        self.assertEqual(1, len(calls))
        self.assertTrue(
            any(step.kind == "sleep" and step.value == "300" for step in calls[0][1])
        )
        self.assertTrue(any("cooldown" in item for item in messages))

    def test_distinct_fatal_incidents_during_cooldown_keep_their_order(self):
        calls = []
        incidents = iter(
            [
                ("turn-503-a", "retryable_http_503"),
                ("turn-503-b", "retryable_http_503"),
            ]
        )

        run_monitor(
            lines=[
                "Pursuing goal (4m)\n",
                "■ unexpected status 503 Service Unavailable: upstream failed\n",
                "Pursuing goal (5m)\n",
                "■ unexpected status 503 Service Unavailable: upstream failed\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID, cooldown_seconds=300),
            recovery_deferred=lambda: True,
            resolve_recovery_incident=lambda _thread_id: next(incidents),
            execute=lambda target, steps: calls.append((target, steps)),
            now=iter([100.0, 101.0, 102.0, 103.0]).__next__,
            log=lambda _message: None,
        )

        assert len(calls) == 2
        assert not any(step.kind == "sleep" and step.value == "300" for step in calls[0][1])
        assert any(step.kind == "sleep" and step.value == "300" for step in calls[1][1])

    def test_compaction_timeout_falls_back_to_fresh_thread_rotation(self):
        calls = []
        handoffs = []

        def execute(target, steps):
            calls.append((target, steps))
            if any(step.kind == "wait_compaction" for step in steps):
                raise TimeoutError("compaction timed out")

        run_monitor(
            lines=[
                "Pursuing goal (4m)\n",
                "■ stream disconnected before completion: codex upstream stalled: "
                "no real data for 5m0s, connection recycled\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID),
            resolve_goal_objective=lambda thread_id: "Goal ID: FE-CREATOR-8",
            write_thread_handoff=lambda **kwargs: (
                handoffs.append(kwargs) or Path("/state/handoffs/latest.json")
            ),
            mark_thread_rotation=lambda count: None,
            now=iter([100.0, 101.0]).__next__,
            execute=execute,
            log=lambda message: None,
        )

        self.assertEqual(2, len(calls))
        self.assertTrue(any(step.kind == "wait_compaction" for step in calls[0][1]))
        self.assertFalse(any(step.kind == "wait_compaction" for step in calls[1][1]))
        self.assertEqual("compaction_timeout", handoffs[0]["reason"])

if __name__ == "__main__":
    unittest.main()
