import io
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_goal_watchdog.monitor import (
    _save_tmux_thread_id,
    iter_decoded_chunks,
    run_monitor,
)
from codex_goal_watchdog.recovery import RecoveryConfig


THREAD_ID = "550e8400-e29b-41d4-a716-446655440000"


class MonitorTests(unittest.TestCase):
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

    def test_run_monitor_recovers_context_window_exhaustion(self):
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

        self.assertEqual(1, len(calls))
        self.assertIn("/compact", [step.value for step in calls[0][1]])

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
        )


if __name__ == "__main__":
    unittest.main()
