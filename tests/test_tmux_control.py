import unittest

from codex_goal_watchdog.recovery import RecoveryStep
from codex_goal_watchdog.tmux_control import (
    capture_update_prompt_version,
    commands_for_step,
    ensure_codex_version,
    handle_goal_prompt,
    monitor_pipe_command,
    update_prompt_version,
    wait_for_pane_state,
)


class TmuxControlTests(unittest.TestCase):
    def test_update_prompt_version_requires_complete_update_picker(self):
        screen = (
            "Update available! 0.144.6 -> 0.145.0\n"
            "1. Update now (runs `npm install -g @openai/codex`)\n"
            "2. Skip\n"
            "3. Skip until next version\n"
        )

        self.assertEqual("0.145.0", update_prompt_version(screen))
        self.assertIsNone(update_prompt_version("Update available! in documentation"))

    def test_capture_update_prompt_version_reads_tmux_screen(self):
        def runner(command, **kwargs):
            class Result:
                stdout = (
                    "Update available! 0.144.6 -> 0.145.0\n"
                    "1. Update now (runs `npm install -g @openai/codex`)\n"
                    "2. Skip\n3. Skip until next version\n"
                )

            return Result()

        self.assertEqual(
            "0.145.0",
            capture_update_prompt_version("codex-goal", runner=runner),
        )

    def test_capture_update_prompt_version_ignores_transcript_quote(self):
        def runner(command, **kwargs):
            class Result:
                stdout = (
                    "OpenAI Codex\n"
                    "Quoted: Update available! 0.144.6 -> 0.145.0\n"
                    "1. Update now (runs `npm install -g @openai/codex`)\n"
                    "2. Skip\n3. Skip until next version\n"
                )

            return Result()

        self.assertIsNone(
            capture_update_prompt_version("codex-goal", runner=runner)
        )

    def test_ensure_codex_version_retries_real_update_when_still_old(self):
        calls = []
        versions = iter(["codex-cli 0.144.6\n", "codex-cli 0.145.0\n"])

        def runner(command, **kwargs):
            calls.append(command)

            class Result:
                returncode = 0
                stdout = next(versions) if command == ["codex", "--version"] else ""

            return Result()

        ensure_codex_version("0.145.0", runner=runner)

        self.assertEqual(
            [
                ["codex", "--version"],
                ["codex", "update"],
                ["codex", "--version"],
            ],
            calls,
        )

    def test_ensure_codex_version_rejects_false_success(self):
        def runner(command, **kwargs):
            class Result:
                returncode = 0
                stdout = "codex-cli 0.144.6\n" if command == ["codex", "--version"] else ""

            return Result()

        with self.assertRaisesRegex(RuntimeError, "expected at least 0.145.0"):
            ensure_codex_version("0.145.0", runner=runner)

    def test_handle_goal_prompt_leaves_goal_paused_for_compaction(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)

            class Result:
                stdout = "Resume paused goal?\n1. Resume goal\n2. Leave paused\n"

            return Result()

        handled = handle_goal_prompt(
            "codex-goal",
            action="leave_paused",
            prompt="",
            runner=runner,
        )

        self.assertTrue(handled)
        self.assertEqual(
            [
                ["tmux", "capture-pane", "-p", "-t", "codex-goal"],
                ["tmux", "send-keys", "-t", "codex-goal", "Down"],
                ["tmux", "send-keys", "-t", "codex-goal", "Enter"],
            ],
            calls,
        )

    def test_handle_goal_prompt_sends_fallback_prompt_when_no_picker(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)

            class Result:
                stdout = "gpt-5.6-sol max"

            return Result()

        handled = handle_goal_prompt(
            "codex-goal",
            action="resume",
            prompt="继续 goal",
            timeout_seconds=0,
            runner=runner,
        )

        self.assertFalse(handled)
        self.assertEqual(
            [
                ["tmux", "capture-pane", "-p", "-t", "codex-goal"],
                ["tmux", "send-keys", "-t", "codex-goal", "-l", "继续 goal"],
                ["tmux", "send-keys", "-t", "codex-goal", "Enter"],
            ],
            calls,
        )

    def test_handle_goal_prompt_can_skip_fallback_when_no_picker(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)

            class Result:
                stdout = "gpt-5.6-sol max"

            return Result()

        handled = handle_goal_prompt(
            "codex-goal",
            action="resume",
            prompt="继续 goal",
            timeout_seconds=0,
            send_fallback_prompt=False,
            runner=runner,
        )

        self.assertFalse(handled)
        self.assertEqual(
            [["tmux", "capture-pane", "-p", "-t", "codex-goal"]],
            calls,
        )

    def test_handle_goal_prompt_ignores_plain_text_picker_mention(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)

            class Result:
                stdout = "The text Resume paused goal? may appear in documentation."

            return Result()

        handled = handle_goal_prompt(
            "codex-goal",
            action="resume",
            prompt="",
            timeout_seconds=0,
            send_fallback_prompt=False,
            runner=runner,
        )

        self.assertFalse(handled)
        self.assertEqual(
            [["tmux", "capture-pane", "-p", "-t", "codex-goal"]],
            calls,
        )

    def test_handle_goal_prompt_waits_for_delayed_picker(self):
        calls = []
        captures = iter(
            [
                "Loading conversation history...",
                "Resume paused goal?\n1. Resume goal\n2. Leave paused\n",
            ]
        )

        def runner(command, **kwargs):
            calls.append(command)

            class Result:
                stdout = next(captures) if command[1] == "capture-pane" else ""

            return Result()

        sleeps = []
        handled = handle_goal_prompt(
            "codex-goal",
            action="resume",
            prompt="继续 goal",
            timeout_seconds=30,
            poll_seconds=0.25,
            runner=runner,
            sleeper=sleeps.append,
            now=iter([0.0, 0.1, 0.2]).__next__,
        )

        self.assertTrue(handled)
        self.assertEqual([0.25], sleeps)
        self.assertEqual(
            [
                ["tmux", "capture-pane", "-p", "-t", "codex-goal"],
                ["tmux", "capture-pane", "-p", "-t", "codex-goal"],
                ["tmux", "send-keys", "-t", "codex-goal", "Enter"],
            ],
            calls,
        )

    def test_handle_goal_prompt_does_not_interrupt_active_goal(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)

            class Result:
                stdout = "Working (12s)\nPursuing goal (4K / 50K)"

            return Result()

        handled = handle_goal_prompt(
            "codex-goal",
            action="resume",
            prompt="继续 goal",
            timeout_seconds=30,
            runner=runner,
        )

        self.assertTrue(handled)
        self.assertEqual(
            [["tmux", "capture-pane", "-p", "-t", "codex-goal"]],
            calls,
        )

    def test_handle_goal_prompt_resumes_paused_or_blocked_goal(self):
        for status in ("Goal paused (/goal resume)", "Goal blocked (/goal resume)"):
            with self.subTest(status=status):
                calls = []

                def runner(command, **kwargs):
                    calls.append(command)

                    class Result:
                        stdout = status

                    return Result()

                handled = handle_goal_prompt(
                    "codex-goal",
                    action="resume",
                    prompt="继续 goal",
                    runner=runner,
                )

                self.assertTrue(handled)
                self.assertEqual(
                    [
                        ["tmux", "capture-pane", "-p", "-t", "codex-goal"],
                        ["tmux", "send-keys", "-t", "codex-goal", "-l", "/goal resume"],
                        ["tmux", "send-keys", "-t", "codex-goal", "Enter"],
                    ],
                    calls,
                )

    def test_wait_for_pane_state_waits_until_shell_is_ready(self):
        process_outputs = iter(
            [
                "100 1 bash bash\n101 100 node node /usr/bin/codex\n",
                "100 1 bash bash\n101 100 node node /usr/bin/codex\n",
                "100 1 bash bash\n",
            ]
        )

        def runner(command, **kwargs):
            class Result:
                stdout = (
                    "100\tbash\n"
                    if command[0] == "tmux"
                    else next(process_outputs)
                )

            return Result()

        wait_for_pane_state(
            "codex-goal",
            state="shell",
            timeout_seconds=10,
            runner=runner,
            sleeper=lambda seconds: None,
            now=iter([0.0, 0.1, 0.2, 0.3]).__next__,
        )

    def test_wait_for_pane_state_accepts_node_as_codex(self):
        def runner(command, **kwargs):
            class Result:
                stdout = (
                    "100\tbash\n"
                    if command[0] == "tmux"
                    else "100 1 bash bash\n101 100 node node /usr/bin/codex\n"
                )

            return Result()

        wait_for_pane_state(
            "codex-goal",
            state="codex",
            timeout_seconds=10,
            runner=runner,
            sleeper=lambda seconds: None,
            now=iter([0.0, 0.1]).__next__,
        )

    def test_wait_for_pane_state_does_not_treat_shell_parent_as_ready(self):
        def runner(command, **kwargs):
            class Result:
                stdout = (
                    "100\tbash\n"
                    if command[0] == "tmux"
                    else "100 1 bash bash\n101 100 node node /usr/bin/codex\n"
                )

            return Result()

        with self.assertRaisesRegex(TimeoutError, "did not reach shell"):
            wait_for_pane_state(
                "codex-goal",
                state="shell",
                timeout_seconds=0,
                runner=runner,
                sleeper=lambda seconds: None,
                now=iter([0.0, 0.1]).__next__,
            )

    def test_commands_for_key_step(self):
        commands = commands_for_step("codex-goal", RecoveryStep("key", "C-c"))

        self.assertEqual([["tmux", "send-keys", "-t", "codex-goal", "C-c"]], commands)

    def test_commands_for_text_step_uses_literal_input_then_enter(self):
        commands = commands_for_step(
            "codex-goal", RecoveryStep("text", "/compact")
        )

        self.assertEqual(
            [
                ["tmux", "send-keys", "-t", "codex-goal", "-l", "/compact"],
                ["tmux", "send-keys", "-t", "codex-goal", "Enter"],
            ],
            commands,
        )

    def test_monitor_pipe_command_quotes_paths_and_prompt(self):
        command = monitor_pipe_command(
            root_dir="/opt/codex-goal-watchdog",
            python_executable="/opt/codex-watch/bin/python",
            session="codex-goal",
            thread_id="550e8400-e29b-41d4-a716-446655440000",
            primary_model="gpt-5.6-sol",
            primary_reasoning_effort="max",
            compact_model="gpt-5.6-luna",
            compact_reasoning_effort="xhigh",
            codex_args=["--dangerously-bypass-approvals-and-sandbox"],
            resume_prompt="继续 goal",
            log_path="/tmp/codex watchdog.log",
        )

        self.assertIn("PYTHONPATH=/opt/codex-goal-watchdog", command)
        self.assertIn(
            "/opt/codex-watch/bin/python -m codex_goal_watchdog monitor",
            command,
        )
        self.assertIn("--session codex-goal", command)
        self.assertIn(
            "--thread-id 550e8400-e29b-41d4-a716-446655440000", command
        )
        self.assertIn("--primary-model gpt-5.6-sol", command)
        self.assertIn("--primary-reasoning-effort max", command)
        self.assertIn("--compact-model gpt-5.6-luna", command)
        self.assertIn("--compact-reasoning-effort xhigh", command)
        self.assertIn("--codex-args-json", command)
        self.assertIn("dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn("--resume-prompt", command)
        self.assertIn("--cooldown-seconds 300", command)
        self.assertIn("--max-recoveries 0", command)
        self.assertIn(">> '/tmp/codex watchdog.log' 2>&1", command)


if __name__ == "__main__":
    unittest.main()
