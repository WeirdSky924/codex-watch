import unittest

from codex_goal_watchdog.recovery import (
    RecoveryConfig,
    RecoveryController,
    RecoveryStep,
    build_post_update_restart_steps,
    build_recovery_steps,
    classify_recovery_reason,
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
        for status in (402, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524):
            with self.subTest(status=status):
                self.assertEqual(
                    f"retryable_http_{status}",
                    classify_recovery_reason(
                        f"■ unexpected status {status} Bad Gateway: upstream request failed"
                    ),
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

    def test_classifies_context_window_exhaustion_on_terminal_fatal_row(self):
        self.assertEqual(
            "context_window_exhausted",
            classify_recovery_reason(
                "■ Codex ran out of room in the model's context window. "
                "Start a new thread or clear earlier history before retrying."
            ),
        )

    def test_ignores_context_window_message_without_terminal_fatal_marker(self):
        self.assertIsNone(
            classify_recovery_reason(
                "Codex ran out of room in the model's context window. "
                "Start a new thread or clear earlier history before retrying."
            )
        )


class RecoveryStepTests(unittest.TestCase):
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

        self.assertEqual("text", steps[0].kind)
        self.assertIn("gpt-5.6-sol", steps[0].value)
        self.assertIn(config.thread_id, steps[0].value)
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
                    "env -u NO_COLOR COLORTERM=truecolor "
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
                    "env -u NO_COLOR COLORTERM=truecolor "
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
            "context_window_exhausted",
            "retryable_http_402",
            "retryable_http_502",
            "retryable_network",
            "retryable_upstream_error",
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

    def test_context_window_exhaustion_compacts_with_luna_then_resumes_sol(self):
        config = RecoveryConfig(
            thread_id="550e8400-e29b-41d4-a716-446655440000",
            compact_model="gpt-5.6-luna",
            compact_reasoning_effort="xhigh",
            primary_model="gpt-5.6-sol",
            primary_reasoning_effort="max",
            codex_args=("--dangerously-bypass-approvals-and-sandbox",),
            compact_wait_seconds=600,
            resume_prompt="继续上下文耗尽前的 goal。",
        )

        steps = build_recovery_steps(config, reason="context_window_exhausted")
        values = [step.value for step in steps]

        self.assertIn("/compact", values)
        self.assertTrue(any("gpt-5.6-luna" in value for value in values))
        self.assertTrue(any("gpt-5.6-sol" in value for value in values))
        self.assertEqual("mark_compaction", steps[9].kind)
        self.assertEqual(config.thread_id, steps[9].value)
        self.assertEqual("wait_compaction", steps[11].kind)
        self.assertEqual(600, steps[11].timeout_seconds)
        self.assertEqual("resume_goal_or_prompt", steps[-1].kind)


if __name__ == "__main__":
    unittest.main()
