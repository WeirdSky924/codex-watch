import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_goal_watchdog.guardian import (
    UPDATE_COMPLETION_CONSUMED_OPTION,
    _RecoveryContentionLogger,
    _guardian_update_restart_needed,
    _missing_codex_recovery_required,
    _next_recovery_attempt,
    _pending_rotation_prompt,
    _recovery_config,
    _recovery_reason_on_screen,
    _restart_pinned_thread_after_update,
    _update_target_version_on_screen,
    recovery_goal_state_on_screen,
    _unhandled_recovery_incident_on_screen,
    _update_completed_on_shell,
    guard_once,
)
from codex_goal_watchdog.bindings import SessionBinding, save_thread_handoff
from codex_goal_watchdog.recovery import RecoveryConfig


class GuardianTests(unittest.TestCase):
    def test_contention_logger_collapses_repeated_owner_messages(self):
        messages = []
        logger = _RecoveryContentionLogger(messages.append)

        logger.record(operation="missing Codex", detail="recovery-owner")
        logger.record(operation="missing Codex", detail="recovery-owner")
        logger.record(operation="missing Codex", detail="recovery-owner")
        logger.flush()

        self.assertEqual(2, len(messages))
        self.assertIn("recovery-owner", messages[0])
        self.assertIn("suppressed=2", messages[1])

    def test_recovery_config_prefers_persistent_binding(self):
        persistent_thread_id = "550e8400-e29b-41d4-a716-446655440001"
        binding = SessionBinding(
            session="codex-goal",
            thread_id=persistent_thread_id,
            cwd=Path("/workspace/project"),
        )

        with patch(
            "codex_goal_watchdog.guardian.load_session_binding",
            return_value=binding,
        ):
            config = _recovery_config(
                "codex-goal",
                option_getter=lambda session, name, default="": (
                    "550e8400-e29b-41d4-a716-446655440000"
                    if name == "@codex_thread_id"
                    else default
                ),
            )

        self.assertEqual(persistent_thread_id, config.thread_id)

    def test_achieved_goal_blocks_pending_missing_codex_recovery(self):
        self.assertFalse(
            _missing_codex_recovery_required(
                goal_state="achieved",
                pending_rotation=True,
                verification_pending=True,
            )
        )
        self.assertTrue(
            _missing_codex_recovery_required(
                goal_state="pursuing",
                pending_rotation=False,
                verification_pending=True,
            )
        )

    def test_guardian_recovers_completed_update_with_pending_version(self):
        self.assertTrue(
            _guardian_update_restart_needed(
                "codex-goal",
                option_getter=lambda session, name, default="": (
                    "" if name == UPDATE_COMPLETION_CONSUMED_OPTION else "0.145.0"
                ),
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

    @patch("codex_goal_watchdog.guardian._mark_verification_pending")
    @patch("codex_goal_watchdog.guardian._next_recovery_attempt")
    def test_update_restart_does_not_enter_fatal_retry_state(
        self,
        next_recovery_attempt_mock,
        mark_verification_pending_mock,
    ):
        executed = []
        config = RecoveryConfig(
            thread_id="550e8400-e29b-41d4-a716-446655440000",
            cooldown_seconds=300,
        )

        _restart_pinned_thread_after_update(
            "codex-goal",
            config,
            expected_version="0.145.0",
            goal_state="pursuing",
            execute_steps=lambda session, steps: executed.append((session, steps)),
        )

        next_recovery_attempt_mock.assert_not_called()
        mark_verification_pending_mock.assert_not_called()
        self.assertEqual("codex-goal", executed[0][0])
        steps = executed[0][1]
        self.assertEqual("ensure_codex_version", steps[0].kind)
        self.assertEqual("0.145.0", steps[0].value)
        self.assertNotIn("300", [step.value for step in steps])

    def test_update_restart_does_not_recover_an_achieved_goal(self):
        executed = []

        _restart_pinned_thread_after_update(
            "codex-goal",
            RecoveryConfig(
                thread_id="550e8400-e29b-41d4-a716-446655440000"
            ),
            expected_version="0.145.0",
            goal_state="achieved",
            execute_steps=lambda session, steps: executed.extend(steps),
        )

        self.assertNotIn(
            "leave_goal_paused",
            [step.kind for step in executed],
        )
        self.assertNotIn(
            "resume_goal_or_prompt",
            [step.kind for step in executed],
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
        self.assertEqual(["restart", "consume"], calls)

    def test_guard_once_does_not_consume_update_when_restart_is_contended(self):
        calls = []

        status = guard_once(
            session_exists=lambda: True,
            pipe_active=lambda: True,
            stalled_screen=lambda: False,
            recover=lambda: calls.append("recover"),
            attach_monitor=lambda: calls.append("attach"),
            update_restart_needed=lambda: True,
            consume_update_completion=lambda: calls.append("consume"),
            restart_after_update=lambda: False,
        )

        self.assertEqual("recovery_in_progress", status)
        self.assertEqual([], calls)

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

    def test_guard_once_retries_pending_submission_before_reporting_health(self):
        calls = []

        status = guard_once(
            session_exists=lambda: True,
            pipe_active=lambda: True,
            codex_running=lambda: True,
            pending_submission_recovery_needed=lambda: True,
            recover_pending_submission=lambda: calls.append("retry") or True,
            stalled_screen=lambda: False,
            recover=lambda: calls.append("recover"),
            attach_monitor=lambda: calls.append("attach"),
        )

        self.assertEqual("recovered", status)
        self.assertEqual(["retry"], calls)

    def test_guard_once_reports_recovery_in_progress_when_callback_cannot_claim(self):
        status = guard_once(
            session_exists=lambda: True,
            pipe_active=lambda: True,
            codex_running=lambda: True,
            pending_submission_recovery_needed=lambda: True,
            recover_pending_submission=lambda: False,
            stalled_screen=lambda: False,
            recover=lambda: None,
            attach_monitor=lambda: None,
        )

        self.assertEqual("recovery_in_progress", status)

    def test_guard_once_reattaches_monitor_after_pending_submission(self):
        calls = []

        status = guard_once(
            session_exists=lambda: True,
            pipe_active=lambda: False,
            codex_running=lambda: True,
            pending_submission_recovery_needed=lambda: True,
            recover_pending_submission=lambda: True,
            stalled_screen=lambda: False,
            recover=lambda: None,
            attach_monitor=lambda: calls.append("attach"),
        )

        self.assertEqual("recovered_and_reattached", status)
        self.assertEqual(["attach"], calls)

    def test_pending_rotation_prompt_uses_bounded_handoff(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_root = Path(temp_dir)
            with patch("codex_goal_watchdog.bindings.state_dir", return_value=state_root):
                handoff = save_thread_handoff(
                    session="codex-goal",
                    thread_id="550e8400-e29b-41d4-a716-446655440000",
                    cwd=Path("/workspace/project"),
                    reason="compaction_timeout",
                    goal_objective="Goal ID: FE-CREATOR-8",
                    telemetry={},
                    state_root=state_root,
                )
                with patch(
                    "codex_goal_watchdog.guardian.recovery_goal_state_on_screen",
                    return_value="pursuing",
                ):
                    prompt = _pending_rotation_prompt(
                        "codex-goal",
                        RecoveryConfig(
                            thread_id="550e8400-e29b-41d4-a716-446655440000"
                        ),
                    )

        self.assertIsNotNone(prompt)
        assert prompt is not None
        self.assertIn(str(handoff), prompt)
        self.assertIn("Goal ID: FE-CREATOR-8", prompt)

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

    def test_completed_update_reads_target_version_from_picker_history(self):
        def runner(command, **kwargs):
            class Result:
                returncode = 0
                stdout = (
                    "• Previous conversation output\n"
                    "Update available! 0.152.0 -> 0.152.1\n"
                    "1. Update now (runs `npm install -g @openai/codex`)\n"
                    "2. Skip\n3. Skip until next version\n"
                    "Update ran successfully! Please restart Codex.\n"
                    "(base) root@host:/workspace#\n"
                )

            return Result()

        self.assertEqual(
            "0.152.1",
            _update_target_version_on_screen("codex-goal", runner=runner),
        )

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
