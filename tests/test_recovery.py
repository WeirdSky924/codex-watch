import json
import unittest

from codex_goal_watchdog.recovery import (
    RecoveryConfig,
    RecoveryController,
    RecoveryStep,
    build_codex_update_steps,
    build_post_update_restart_steps,
    build_recovery_steps,
    build_startup_update_steps,
    classify_recovery_message,
    classify_recovery_reason,
    thread_rotation_reason,
)


class RecoveryControllerTests(unittest.TestCase):
    def test_detects_codex_upstream_stall_once(self):
        controller = RecoveryController(
            RecoveryConfig(cooldown_seconds=60, max_recoveries=3)
        )

        event = controller.observe(
            "■ stream disconnected before completion: codex upstream stalled: "
            "no real data for 5m0s, connection recycled",
            now=100.0,
        )

        self.assertIsNotNone(event)
        self.assertEqual("codex_upstream_stalled", event.reason)
        self.assertEqual(1, controller.recovery_count)

    def test_cooldown_does_not_drop_later_fatal_events(self):
        controller = RecoveryController(
            RecoveryConfig(cooldown_seconds=120, max_recoveries=3)
        )
        first = controller.observe(
            "■ codex upstream stalled: no real data for 5m0s, connection recycled",
            now=10.0,
        )
        second = controller.observe(
            "■ codex upstream stalled: no real data for 5m0s, connection recycled",
            now=30.0,
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(2, controller.recovery_count)

    def test_stops_after_max_recoveries(self):
        controller = RecoveryController(
            RecoveryConfig(cooldown_seconds=0, max_recoveries=1)
        )
        first = controller.observe(
            "■ codex upstream stalled: no real data for 5m0s, connection recycled",
            now=10.0,
        )
        second = controller.observe(
            "■ codex upstream stalled: no real data for 5m0s, connection recycled",
            now=11.0,
        )

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(1, controller.recovery_count)

    def test_zero_max_recoveries_allows_unlimited_attempts(self):
        controller = RecoveryController(
            RecoveryConfig(cooldown_seconds=0, max_recoveries=0)
        )

        events = [
            controller.observe(
                "■ unexpected status 502 Bad Gateway: upstream request failed",
                now=float(index),
            )
            for index in range(1, 11)
        ]

        self.assertTrue(all(event is not None for event in events))
        self.assertEqual(10, controller.recovery_count)

    def test_classifies_retryable_terminal_http_errors(self):
        for status in (401, 402, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524):
            with self.subTest(status=status):
                self.assertEqual(
                    f"retryable_http_{status}",
                    classify_recovery_reason(
                        f"■ unexpected status {status} Bad Gateway: upstream request failed"
                    ),
                )

    def test_classifies_upstream_access_denied_as_thread_ban(self):
        message = (
            "unexpected status 502 Bad Gateway: Upstream access denied, "
            "url: http://localhost:54322/responses"
        )

        self.assertEqual(
            "upstream_access_denied",
            classify_recovery_message(message),
        )
        self.assertEqual(
            "upstream_access_denied",
            classify_recovery_reason(f"■ {message}"),
        )

    def test_latest_terminal_fatal_wins_for_guardian_screen_recovery(self):
        screen = (
            "■ unexpected status 503 Service Unavailable: upstream failed\n"
            "■ unexpected status 502 Bad Gateway: Upstream access denied\n"
        )

        self.assertEqual(
            "upstream_access_denied",
            classify_recovery_reason(screen),
        )

    def test_classifies_terminal_401_api_disabled(self):
        for message in (
            "■ unexpected status 401 Unauthorized: API DISABLE",
            "■ 401 API DISABLE",
            "■ API disabled: status 401",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    "retryable_http_401",
                    classify_recovery_reason(message),
                )

    def test_does_not_classify_http_codes_without_terminal_error_marker(self):
        self.assertIsNone(
            classify_recovery_reason(
                "Agent errored: unexpected status 502 Bad Gateway; retrying worker"
            )
        )

    def test_classifies_upstream_error_json_on_terminal_fatal_row(self):
        self.assertEqual(
            "retryable_upstream_error",
            classify_recovery_reason(
                '■ {"error":{"message":"Upstream request failed",'
                '"type":"upstream_error"}}'
            ),
        )

    def test_ignores_upstream_error_json_outside_exact_terminal_fatal_shape(self):
        upstream_error = (
            '{"error":{"message":"Upstream request failed",'
            '"type":"upstream_error"}}'
        )

        self.assertIsNone(classify_recovery_reason(upstream_error))
        self.assertIsNone(
            classify_recovery_reason(
                '■ {"error":{"message":"A different failure",'
                '"type":"upstream_error"}}'
            )
        )

    def test_ignores_context_window_exhaustion_for_codex_to_handle(self):
        self.assertIsNone(
            classify_recovery_reason(
                "■ Codex ran out of room in the model's context window. "
                "Start a new thread or clear earlier history before retrying."
            )
        )

    def test_ignores_context_window_message_without_terminal_fatal_marker(self):
        self.assertIsNone(
            classify_recovery_reason(
                "Codex ran out of room in the model's context window. "
                "Start a new thread or clear earlier history before retrying."
            )
        )

    def test_classifies_model_capacity_terminal_warning(self):
        self.assertEqual(
            "model_at_capacity",
            classify_recovery_reason(
                "⚠ Selected model is at capacity. Please try a different model"
            ),
        )

    def test_classifies_structured_rollout_failure_messages(self):
        self.assertEqual(
            "retryable_http_503",
            classify_recovery_message(
                "unexpected status 503 Service Unavailable: "
                "Service temporarily unavailable"
            ),
        )
        self.assertEqual(
            "model_at_capacity",
            classify_recovery_message(
                "Selected model is at capacity. Please try a different model."
            ),
        )

    def test_classifies_server_overload_rollout_failure_message(self):
        message = (
            "stream disconnected before completion: Our servers are currently "
            "overloaded. Please try again later."
        )

        self.assertEqual(
            "servers_overloaded",
            classify_recovery_message(message),
        )
        self.assertEqual(
            "servers_overloaded",
            classify_recovery_reason(f"■ {message}"),
        )

    def test_ignores_model_capacity_message_without_terminal_warning_marker(self):
        self.assertIsNone(
            classify_recovery_reason(
                "Selected model is at capacity. Please try a different model"
            )
        )


class RecoveryStepTests(unittest.TestCase):
    def test_startup_update_verifies_version_before_starting_fresh_codex(self):
        codex_command = ["codex", "--no-alt-screen", "-m", "gpt-5.6-sol"]

        steps = build_startup_update_steps(codex_command, "0.145.0")

        self.assertEqual("key", steps[0].kind)
        self.assertEqual("1", steps[0].value)
        self.assertEqual("wait_shell", steps[1].kind)
        self.assertEqual(RecoveryStep("ensure_codex_version", "0.145.0"), steps[2])
        self.assertEqual("codex --no-alt-screen -m gpt-5.6-sol", steps[3].value)
        self.assertEqual("wait_codex", steps[4].kind)

    def test_thread_update_resumes_pinned_goal_after_version_verification(self):
        config = RecoveryConfig(
            thread_id="550e8400-e29b-41d4-a716-446655440000",
            primary_model="gpt-5.6-sol",
            primary_reasoning_effort="max",
            resume_prompt="继续更新前的 goal。",
        )

        steps = build_codex_update_steps(config, "0.145.0")

        self.assertEqual(RecoveryStep("key", "1"), steps[0])
        self.assertEqual(RecoveryStep("ensure_codex_version", "0.145.0"), steps[2])
        self.assertIn(config.thread_id, steps[3].value)
        self.assertEqual("resume_goal_or_prompt", steps[-1].kind)

    def test_post_update_restart_resumes_sol_directly_from_shell(self):
        config = RecoveryConfig(
            thread_id="550e8400-e29b-41d4-a716-446655440000",
            primary_model="gpt-5.6-sol",
            primary_reasoning_effort="max",
            codex_args=("--dangerously-bypass-approvals-and-sandbox",),
            startup_wait_seconds=5,
            resume_prompt="继续更新前的 goal。",
        )

        steps = build_post_update_restart_steps(config)
        values = [step.value for step in steps]

        self.assertEqual(RecoveryStep("update_codex", ""), steps[0])
        self.assertEqual("text", steps[1].kind)
        self.assertIn("gpt-5.6-sol", steps[1].value)
        self.assertIn(config.thread_id, steps[1].value)
        self.assertNotIn("C-c", values)
        self.assertNotIn("/quit", values)
        self.assertNotIn("/compact", values)
        self.assertEqual("resume_goal_or_prompt", steps[-1].kind)
        self.assertEqual(config.resume_prompt, steps[-1].value)

    def test_build_recovery_steps_switches_compacts_and_resumes(self):
        config = RecoveryConfig(
            thread_id="550e8400-e29b-41d4-a716-446655440000",
            compact_model="gpt-5.6-luna",
            compact_reasoning_effort="xhigh",
            primary_model="gpt-5.6-sol",
            primary_reasoning_effort="max",
            codex_args=("--dangerously-bypass-approvals-and-sandbox",),
            abort_delay_seconds=2,
            quit_wait_seconds=4,
            startup_wait_seconds=5,
            model_switch_delay_seconds=3,
            compact_wait_seconds=90,
            cooldown_seconds=300,
            resume_prompt="继续刚才被 5m0s 中断的 goal。",
        )

        steps = build_recovery_steps(config)

        self.assertEqual(
            [
                RecoveryStep("key", "C-c"),
                RecoveryStep("sleep", "2"),
                RecoveryStep("text", "/quit"),
                RecoveryStep("wait_shell", "30"),
                RecoveryStep("sleep", "0"),
                RecoveryStep(
                    "text",
                    "env -u NO_COLOR -u CODEX_THREAD_ID -u CODEX_CI "
                    "COLORTERM=truecolor "
                    "codex --no-alt-screen -m gpt-5.6-luna -c "
                    "'model_reasoning_effort=\"xhigh\"' "
                    "--dangerously-bypass-approvals-and-sandbox resume "
                    "550e8400-e29b-41d4-a716-446655440000",
                ),
                RecoveryStep("wait_codex", "30"),
                RecoveryStep("sleep", "5"),
                RecoveryStep("leave_goal_paused", ""),
                RecoveryStep(
                    "mark_compaction",
                    "550e8400-e29b-41d4-a716-446655440000",
                ),
                RecoveryStep("text", "/compact"),
                RecoveryStep(
                    "wait_compaction",
                    "550e8400-e29b-41d4-a716-446655440000",
                    timeout_seconds=90,
                ),
                RecoveryStep("text", "/quit"),
                RecoveryStep("wait_shell", "30"),
                RecoveryStep("sleep", "1"),
                RecoveryStep(
                    "text",
                    "env -u NO_COLOR -u CODEX_THREAD_ID -u CODEX_CI "
                    "COLORTERM=truecolor "
                    "codex --no-alt-screen -m gpt-5.6-sol -c "
                    "'model_reasoning_effort=\"max\"' "
                    "--dangerously-bypass-approvals-and-sandbox resume "
                    "550e8400-e29b-41d4-a716-446655440000",
                ),
                RecoveryStep("wait_codex", "30"),
                RecoveryStep("sleep", "5"),
                RecoveryStep(
                    "resume_goal_or_prompt",
                    "继续刚才被 5m0s 中断的 goal。",
                ),
            ],
            steps,
        )

    def test_default_models_match_sol_and_luna_recovery_chain(self):
        config = RecoveryConfig()

        self.assertEqual("gpt-5.6-sol", config.primary_model)
        self.assertEqual("max", config.primary_reasoning_effort)
        self.assertEqual("gpt-5.6-luna", config.compact_model)
        self.assertEqual("xhigh", config.compact_reasoning_effort)

    def test_default_recovery_policy_is_unlimited_with_five_minute_cooldown(self):
        config = RecoveryConfig()

        self.assertEqual(300, config.cooldown_seconds)
        self.assertEqual(0, config.max_recoveries)

    def test_recovery_refuses_to_resume_without_pinned_thread(self):
        with self.assertRaisesRegex(ValueError, "thread ID"):
            build_recovery_steps(RecoveryConfig())

    def test_retryable_http_recovery_restarts_sol_without_compaction(self):
        config = RecoveryConfig(
            thread_id="550e8400-e29b-41d4-a716-446655440000",
            primary_model="gpt-5.6-sol",
            primary_reasoning_effort="max",
            codex_args=("--dangerously-bypass-approvals-and-sandbox",),
            abort_delay_seconds=2,
            startup_wait_seconds=5,
            resume_prompt="继续中断的 goal。",
        )

        steps = build_recovery_steps(config, reason="retryable_http_502")
        values = [step.value for step in steps]

        self.assertNotIn("/compact", values)
        self.assertFalse(any("gpt-5.6-luna" in value for value in values))
        self.assertTrue(any("gpt-5.6-sol" in value for value in values))
        self.assertEqual("resume_goal_or_prompt", steps[-1].kind)

    def test_access_denied_starts_fresh_thread_and_rebuilds_goal(self):
        old_thread_id = "550e8400-e29b-41d4-a716-446655440000"
        config = RecoveryConfig(
            thread_id=old_thread_id,
            primary_model="gpt-5.6-sol",
            primary_reasoning_effort="max",
            codex_args=("--dangerously-bypass-approvals-and-sandbox",),
            cooldown_seconds=300,
        )
        objective = "Goal ID: FE-CREATOR-8\n关闭当前唯一 ACTIVE 计划。"

        first_steps = build_recovery_steps(
            config,
            reason="upstream_access_denied",
            recovery_attempt=1,
            goal_objective=objective,
        )
        retry_steps = build_recovery_steps(
            config,
            reason="upstream_access_denied",
            recovery_attempt=2,
            goal_objective=objective,
        )

        self.assertEqual(RecoveryStep("sleep", "0"), first_steps[4])
        self.assertEqual(RecoveryStep("sleep", "300"), retry_steps[4])
        fresh_command = first_steps[5].value
        self.assertIn("gpt-5.6-sol", fresh_command)
        self.assertNotIn(" resume ", fresh_command)
        self.assertNotIn(old_thread_id, fresh_command)
        self.assertEqual("text", first_steps[-1].kind)
        self.assertIn(
            json.dumps(objective, ensure_ascii=False),
            first_steps[-1].value,
        )
        self.assertNotIn("\n", first_steps[-1].value)
        self.assertIn("当前工作树", first_steps[-1].value)
        self.assertIn("唯一 ACTIVE Plan", first_steps[-1].value)
        self.assertIn("handoff", first_steps[-1].value)

    def test_access_denied_rotation_preserves_blocked_review_boundary(self):
        config = RecoveryConfig(
            thread_id="550e8400-e29b-41d4-a716-446655440000",
        )

        steps = build_recovery_steps(
            config,
            reason="upstream_access_denied",
            resume_goal=False,
            goal_objective="审核后继续",
        )

        self.assertIn("blocked", steps[-1].value)
        self.assertIn("等待用户", steps[-1].value)

    def test_blocked_goal_recovery_restarts_process_but_leaves_goal_paused(self):
        config = RecoveryConfig(
            thread_id="550e8400-e29b-41d4-a716-446655440000",
            primary_model="gpt-5.6-sol",
            resume_prompt="继续中断的 goal。",
        )

        steps = build_recovery_steps(
            config,
            reason="retryable_http_503",
            resume_goal=False,
        )
        values = [step.value for step in steps]

        self.assertTrue(any("gpt-5.6-sol" in value for value in values))
        self.assertNotIn("/goal resume", values)
        self.assertEqual("leave_goal_paused", steps[-1].kind)

    def test_stalled_goal_fatal_recovery_resumes_only_after_process_restart(self):
        config = RecoveryConfig(
            thread_id="550e8400-e29b-41d4-a716-446655440000",
            primary_model="gpt-5.6-sol",
            resume_prompt="继续中断的 goal。",
        )

        steps = build_recovery_steps(
            config,
            reason="retryable_http_503",
            resume_stalled_goal=True,
        )

        self.assertEqual("key", steps[0].kind)
        self.assertEqual("resume_stalled_goal_or_prompt", steps[-1].kind)

    def test_payment_required_waits_five_minutes_then_restarts_sol(self):
        config = RecoveryConfig(
            thread_id="550e8400-e29b-41d4-a716-446655440000",
            primary_model="gpt-5.6-sol",
            primary_reasoning_effort="max",
            codex_args=("--dangerously-bypass-approvals-and-sandbox",),
            cooldown_seconds=300,
            resume_prompt="继续中断的 goal。",
        )

        first_steps = build_recovery_steps(
            config,
            reason="retryable_http_402",
            recovery_attempt=1,
        )
        retry_steps = build_recovery_steps(
            config,
            reason="retryable_http_402",
            recovery_attempt=2,
        )
        values = [step.value for step in retry_steps]

        self.assertEqual(RecoveryStep("sleep", "0"), first_steps[4])
        self.assertEqual(RecoveryStep("wait_shell", "30"), retry_steps[3])
        self.assertEqual(RecoveryStep("sleep", "300"), retry_steps[4])
        self.assertNotIn("/compact", values)
        self.assertTrue(any("gpt-5.6-sol" in value for value in values))
        self.assertEqual("resume_goal_or_prompt", retry_steps[-1].kind)

    def test_all_fatal_recovery_paths_share_immediate_then_delayed_policy(self):
        config = RecoveryConfig(
            thread_id="550e8400-e29b-41d4-a716-446655440000",
            cooldown_seconds=300,
        )

        for reason in (
            "codex_upstream_stalled",
            "retryable_http_402",
            "retryable_http_502",
            "upstream_access_denied",
            "retryable_network",
            "retryable_upstream_error",
            "model_at_capacity",
            "servers_overloaded",
        ):
            with self.subTest(reason=reason):
                first_steps = build_recovery_steps(
                    config,
                    reason=reason,
                    recovery_attempt=1,
                )
                retry_steps = build_recovery_steps(
                    config,
                    reason=reason,
                    recovery_attempt=2,
                )

                self.assertEqual(RecoveryStep("sleep", "0"), first_steps[4])
                self.assertEqual(RecoveryStep("wait_shell", "30"), retry_steps[3])
                self.assertEqual(RecoveryStep("sleep", "300"), retry_steps[4])

    def test_upstream_error_recovery_restarts_sol_without_compaction(self):
        config = RecoveryConfig(
            thread_id="550e8400-e29b-41d4-a716-446655440000",
            primary_model="gpt-5.6-sol",
            primary_reasoning_effort="max",
            codex_args=("--dangerously-bypass-approvals-and-sandbox",),
            resume_prompt="继续中断的 goal。",
        )

        reason = classify_recovery_reason(
            '■ {"error":{"message":"Upstream request failed",'
            '"type":"upstream_error"}}'
        )
        steps = build_recovery_steps(config, reason=reason)
        values = [step.value for step in steps]

        self.assertEqual("retryable_upstream_error", reason)
        self.assertNotIn("/compact", values)
        self.assertFalse(any("gpt-5.6-luna" in value for value in values))
        self.assertTrue(any("gpt-5.6-sol" in value for value in values))
        self.assertEqual("resume_goal_or_prompt", steps[-1].kind)

    def test_thread_health_thresholds_request_rotation(self):
        config = RecoveryConfig(
            thread_max_compactions=3,
            thread_max_rollout_bytes=1_000,
            thread_no_progress_tokens=500,
            thread_no_event_seconds=120,
            thread_max_repeated_content=3,
            thread_max_repeated_commands=3,
        )

        cases = (
            ({"compaction_count": 3}, "max_compactions"),
            ({"rollout_bytes": 1_000}, "max_rollout_bytes"),
            (
                {"total_tokens": 1_500, "tokens_at_last_progress": 1_000},
                "no_progress_tokens",
            ),
            ({"repeated_content_count": 3}, "repeated_content"),
            ({"repeated_command_count": 3}, "repeated_command"),
            ({"last_event_age_seconds": 120}, "no_rollout_events"),
        )
        defaults = {
            "compaction_count": 0,
            "rollout_bytes": 0,
            "context_tokens": 0,
            "total_tokens": 0,
            "tokens_at_last_progress": 0,
            "repeated_content_count": 0,
            "repeated_command_count": 0,
            "last_event_age_seconds": 0,
        }

        for values, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    expected,
                    thread_rotation_reason(config, **(defaults | values)),
                )

    def test_context_usage_never_requests_thread_rotation(self):
        config = RecoveryConfig(thread_max_context_tokens=800)

        self.assertIsNone(
            thread_rotation_reason(
                config,
                compaction_count=0,
                rollout_bytes=0,
                context_tokens=900,
                total_tokens=0,
                tokens_at_last_progress=0,
                last_event_age_seconds=0,
            )
        )

    def test_zero_repetition_thresholds_disable_loop_rotation(self):
        config = RecoveryConfig(
            thread_max_repeated_content=0,
            thread_max_repeated_commands=0,
        )

        self.assertIsNone(
            thread_rotation_reason(
                config,
                compaction_count=0,
                rollout_bytes=0,
                context_tokens=0,
                total_tokens=0,
                tokens_at_last_progress=0,
                last_event_age_seconds=0,
                repeated_content_count=100,
                repeated_command_count=100,
            )
        )

    def test_thread_health_rotation_starts_fresh_thread_with_handoff(self):
        thread_id = "550e8400-e29b-41d4-a716-446655440000"
        config = RecoveryConfig(thread_id=thread_id)

        steps = build_recovery_steps(
            config,
            reason="thread_health_rotation",
            goal_objective="Goal ID: FE-CREATOR-8",
            handoff_path="/state/handoffs/codex-goal.json",
            rotation_detail="max_rollout_bytes",
        )
        values = [step.value for step in steps]

        self.assertFalse(any(f"resume {thread_id}" in value for value in values))
        self.assertNotIn("/compact", values)
        self.assertIn("/state/handoffs/codex-goal.json", values[-1])
        self.assertIn("max_rollout_bytes", values[-1])
        self.assertIn("Goal ID: FE-CREATOR-8", values[-1])


if __name__ == "__main__":
    unittest.main()
