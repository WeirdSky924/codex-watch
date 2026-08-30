import unittest

from codex_goal_watchdog.guardian import (
    UPDATE_COMPLETION_CONSUMED_OPTION,
    _guardian_update_restart_needed,
    _next_recovery_attempt,
    _recovery_config,
    _recovery_reason_on_screen,
    recovery_goal_state_on_screen,
    _unhandled_recovery_incident_on_screen,
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

    def test_guardian_does_not_repeat_consumed_update_completion(self):
        self.assertFalse(
            _guardian_update_restart_needed(
                "codex-goal",
                option_getter=lambda session, name, default="": (
                    "0.151.0"
                    if name == UPDATE_COMPLETION_CONSUMED_OPTION
                    else ""
                ),
                completion_checker=lambda session: True,
            )
        )

    def test_next_recovery_attempt_persists_tmux_count(self):
        calls = []
        persisted = []

        attempt = _next_recovery_attempt(
            "codex-goal",
            option_getter=lambda session, name, default="": "1",
            runner=lambda command, **kwargs: calls.append(command),
            persist_count=lambda session, count: persisted.append(
                (session, count)
            ),
        )

        self.assertEqual(2, attempt)
        self.assertEqual([("codex-goal", 2)], persisted)
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

    def test_guard_once_recovers_visible_stall_with_active_monitor(self):
        calls = []

        status = guard_once(
            session_exists=lambda: True,
            pipe_active=lambda: True,
            stalled_screen=lambda: True,
            recover=lambda: calls.append("recover"),
            attach_monitor=lambda: calls.append("attach"),
        )

        self.assertEqual("recovered", status)
        self.assertEqual(["recover"], calls)

    def test_guard_once_leaves_live_monitor_as_sole_owner_after_handoff(self):
        calls = []

        status = guard_once(
            session_exists=lambda: True,
            pipe_active=lambda: True,
            stalled_screen=lambda: calls.append("scan") or True,
            recover=lambda: calls.append("recover"),
            attach_monitor=lambda: calls.append("attach"),
            inspect_active_screen=False,
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

    def test_guard_once_consumes_update_before_restart(self):
        calls = []

        status = guard_once(
            session_exists=lambda: True,
            pipe_active=lambda: True,
            stalled_screen=lambda: False,
            recover=lambda: calls.append("recover"),
            attach_monitor=lambda: calls.append("attach"),
            update_restart_needed=lambda: True,
            consume_update_completion=lambda: calls.append("consume"),
            restart_after_update=lambda: calls.append("restart"),
        )

        self.assertEqual("restarted_after_update", status)
        self.assertEqual(["consume", "restart"], calls)

    def test_guard_once_clears_consumed_update_when_codex_is_running(self):
        calls = []

        status = guard_once(
            session_exists=lambda: True,
            pipe_active=lambda: True,
            codex_running=lambda: True,
            clear_update_completion=lambda: calls.append("clear"),
            stalled_screen=lambda: False,
            recover=lambda: calls.append("recover"),
            attach_monitor=lambda: calls.append("attach"),
        )

        self.assertEqual("healthy", status)
        self.assertEqual(["clear"], calls)

    def test_guard_once_recovers_when_pipe_is_active_but_codex_is_missing(self):
        calls = []

        status = guard_once(
            session_exists=lambda: True,
            pipe_active=lambda: True,
            codex_running=lambda: False,
            missing_codex_recovery_needed=lambda: True,
            recover_missing_codex=lambda: calls.append("recover_missing"),
            stalled_screen=lambda: False,
            recover=lambda: calls.append("recover"),
            attach_monitor=lambda: calls.append("attach"),
        )

        self.assertEqual("recovered", status)
        self.assertEqual(["recover_missing"], calls)

    def test_guard_once_does_not_call_missing_recovery_without_pending_state(self):
        calls = []

        status = guard_once(
            session_exists=lambda: True,
            pipe_active=lambda: True,
            codex_running=lambda: False,
            missing_codex_recovery_needed=lambda: False,
            recover_missing_codex=lambda: calls.append("recover_missing"),
            stalled_screen=lambda: False,
            recover=lambda: calls.append("recover"),
            attach_monitor=lambda: calls.append("attach"),
        )

        self.assertEqual("codex_missing", status)
        self.assertEqual([], calls)

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

    def test_recovery_config_restores_thread_health_policy(self):
        options = {
            "@codex_thread_id": "550e8400-e29b-41d4-a716-446655440000",
            "@codex_thread_max_compactions": "4",
            "@codex_thread_max_rollout_bytes": "1234",
            "@codex_thread_max_context_tokens": "5678",
            "@codex_thread_no_progress_tokens": "9012",
            "@codex_thread_no_event_seconds": "3456",
            "@codex_thread_health_poll_seconds": "78",
            "@codex_thread_max_repeated_content": "4",
            "@codex_thread_max_repeated_commands": "5",
        }

        config = _recovery_config(
            "codex-goal",
            option_getter=lambda session, name, default="": options.get(name, default),
        )

        self.assertEqual(4, config.thread_max_compactions)
        self.assertEqual(1234, config.thread_max_rollout_bytes)
        self.assertEqual(5678, config.thread_max_context_tokens)
        self.assertEqual(9012, config.thread_no_progress_tokens)
        self.assertEqual(3456, config.thread_no_event_seconds)
        self.assertEqual(78, config.thread_health_poll_seconds)
        self.assertEqual(4, config.thread_max_repeated_content)
        self.assertEqual(5, config.thread_max_repeated_commands)

    def test_visible_recovery_requires_active_or_blocked_goal(self):
        def runner(command, **kwargs):
            class Result:
                returncode = 0
                stdout = (
                    "Goal achieved\n"
                    "■ unexpected status 503 Service Unavailable: upstream failed\n"
                )

            return Result()

        self.assertIsNone(_recovery_reason_on_screen("codex-goal", runner=runner))

    def test_visible_recovery_allows_blocked_goal(self):
        def runner(command, **kwargs):
            class Result:
                returncode = 0
                stdout = (
                    "Goal blocked (/goal resume)\n"
                    "■ unexpected status 503 Service Unavailable: upstream failed\n"
                )

            return Result()

        self.assertEqual(
            "retryable_http_503",
            _recovery_reason_on_screen("codex-goal", runner=runner),
        )

    def test_visible_recovery_allows_stalled_goal_with_fatal_error(self):
        def runner(command, **kwargs):
            class Result:
                returncode = 0
                stdout = (
                    "Goal stalled (/goal resume)\n"
                    "■ unexpected status 503 Service Unavailable: upstream failed\n"
                )

            return Result()

        self.assertEqual(
            "retryable_http_503",
            _recovery_reason_on_screen("codex-goal", runner=runner),
        )

    def test_guardian_reads_blocked_state_before_recovery(self):
        def runner(command, **kwargs):
            class Result:
                returncode = 0
                stdout = (
                    "Pursuing goal\n"
                    "Goal blocked (/goal resume)\n"
                    "■ unexpected status 503 Service Unavailable: upstream failed\n"
                )

            return Result()

        self.assertEqual(
            "blocked",
            recovery_goal_state_on_screen("codex-goal", runner=runner),
        )

    def test_guardian_ignores_already_handled_visible_failure(self):
        incident = _unhandled_recovery_incident_on_screen(
            "codex-goal",
            thread_id="550e8400-e29b-41d4-a716-446655440000",
            screen_reason=lambda session: "retryable_http_503",
            incident_resolver=lambda thread_id: (
                "turn-503",
                "retryable_http_503",
            ),
            option_getter=lambda session, name, default="": "turn-503",
        )

        self.assertIsNone(incident)

    def test_guardian_accepts_new_visible_failure(self):
        incident = _unhandled_recovery_incident_on_screen(
            "codex-goal",
            thread_id="550e8400-e29b-41d4-a716-446655440000",
            screen_reason=lambda session: "retryable_http_503",
            incident_resolver=lambda thread_id: (
                "turn-503-new",
                "retryable_http_503",
            ),
            option_getter=lambda session, name, default="": "turn-503-old",
        )

        self.assertEqual(
            ("turn-503-new", "retryable_http_503"),
            incident,
        )


if __name__ == "__main__":
    unittest.main()
