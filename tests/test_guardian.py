import unittest

from codex_goal_watchdog.guardian import (
    _guardian_update_restart_needed,
    _next_recovery_attempt,
    _recovery_config,
    _update_completed_on_shell,
    guard_once,
)


class GuardianTests(unittest.TestCase):
    def test_guardian_defers_update_restart_while_monitor_owns_update(self):
        self.assertFalse(
            _guardian_update_restart_needed(
                "codex-goal",
                option_getter=lambda session, name, default="": "0.145.0",
                completion_checker=lambda session: True,
            )
        )

    def test_guardian_restarts_legacy_update_without_pending_marker(self):
        self.assertTrue(
            _guardian_update_restart_needed(
                "codex-goal",
                option_getter=lambda session, name, default="": "",
                completion_checker=lambda session: True,
            )
        )

    def test_next_recovery_attempt_persists_tmux_count(self):
        calls = []

        attempt = _next_recovery_attempt(
            "codex-goal",
            option_getter=lambda session, name, default="": "1",
            runner=lambda command, **kwargs: calls.append(command),
        )

        self.assertEqual(2, attempt)
        self.assertEqual(
            [
                [
                    "tmux",
                    "set-option",
                    "-t",
                    "codex-goal",
                    "@codex_recovery_count",
                    "2",
                ]
            ],
            calls,
        )

    def test_guard_once_does_nothing_when_monitor_pipe_is_healthy(self):
        calls = []

        status = guard_once(
            session_exists=lambda: True,
            pipe_active=lambda: True,
            stalled_screen=lambda: False,
            recover=lambda: calls.append("recover"),
            attach_monitor=lambda: calls.append("attach"),
        )

        self.assertEqual("healthy", status)
        self.assertEqual([], calls)

    def test_guard_once_reattaches_missing_monitor(self):
        calls = []

        status = guard_once(
            session_exists=lambda: True,
            pipe_active=lambda: False,
            stalled_screen=lambda: False,
            recover=lambda: calls.append("recover"),
            attach_monitor=lambda: calls.append("attach"),
        )

        self.assertEqual("reattached", status)
        self.assertEqual(["attach"], calls)

    def test_guard_once_recovers_visible_stall_before_reattaching(self):
        calls = []

        status = guard_once(
            session_exists=lambda: True,
            pipe_active=lambda: False,
            stalled_screen=lambda: True,
            recover=lambda: calls.append("recover"),
            attach_monitor=lambda: calls.append("attach"),
        )

        self.assertEqual("recovered_and_reattached", status)
        self.assertEqual(["recover", "attach"], calls)

    def test_guard_once_waits_when_tmux_session_is_missing(self):
        status = guard_once(
            session_exists=lambda: False,
            pipe_active=lambda: False,
            stalled_screen=lambda: False,
            recover=lambda: None,
            attach_monitor=lambda: None,
        )

        self.assertEqual("session_missing", status)

    def test_guard_once_restarts_after_update_even_when_pipe_is_active(self):
        calls = []

        status = guard_once(
            session_exists=lambda: True,
            pipe_active=lambda: True,
            stalled_screen=lambda: False,
            recover=lambda: calls.append("recover"),
            attach_monitor=lambda: calls.append("attach"),
            update_restart_needed=lambda: True,
            restart_after_update=lambda: calls.append("restart_after_update"),
        )

        self.assertEqual("restarted_after_update", status)
        self.assertEqual(["restart_after_update"], calls)

    def test_update_completion_requires_success_marker_and_shell_pane(self):
        def runner(command, **kwargs):
            class Result:
                returncode = 0
                stdout = (
                    "bash\n"
                    if "display-message" in command
                    else "Update ran successfully! Please restart Codex.\n"
                )

            return Result()

        self.assertTrue(_update_completed_on_shell("codex-goal", runner=runner))

    def test_update_completion_ignores_marker_while_codex_is_running(self):
        def runner(command, **kwargs):
            class Result:
                returncode = 0
                stdout = (
                    "node\n"
                    if "display-message" in command
                    else "Update ran successfully! Please restart Codex.\n"
                )

            return Result()

        self.assertFalse(_update_completed_on_shell("codex-goal", runner=runner))

    def test_recovery_config_restores_explicit_retry_policy(self):
        options = {
            "@codex_thread_id": "550e8400-e29b-41d4-a716-446655440000",
            "@codex_cooldown_seconds": "45",
            "@codex_max_recoveries": "7",
        }

        config = _recovery_config(
            "codex-goal",
            option_getter=lambda session, name, default="": options.get(name, default),
        )

        self.assertEqual(45, config.cooldown_seconds)
        self.assertEqual(7, config.max_recoveries)


if __name__ == "__main__":
    unittest.main()
