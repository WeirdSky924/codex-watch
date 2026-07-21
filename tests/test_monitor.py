import io
import unittest

from codex_goal_watchdog.monitor import iter_decoded_chunks, run_monitor
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
                "ordinary output\n",
                "■ stream disconnected before completion: codex upstream stalled: "
                "no real data for 5m0s, connection recycled\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID, cooldown_seconds=0),
            now=lambda: 100.0,
            execute=lambda target, steps: calls.append((target, steps)),
            log=lambda message: None,
        )

        self.assertEqual(1, len(calls))
        self.assertEqual("codex-goal", calls[0][0])
        self.assertEqual("key", calls[0][1][0].kind)

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

    def test_run_monitor_detects_wrapped_ansi_stall_output(self):
        calls = []

        run_monitor(
            lines=[
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

    def test_run_monitor_retries_payment_required_without_attempt_limit(self):
        calls = []

        run_monitor(
            lines=[
                "■ unexpected status 402 Payment Required: upstream request failed\n"
                for _ in range(5)
            ],
            target="codex-goal",
            config=RecoveryConfig(
                thread_id=THREAD_ID,
                cooldown_seconds=300,
                max_recoveries=0,
            ),
            now=iter(float(index) for index in range(5)).__next__,
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


if __name__ == "__main__":
    unittest.main()
