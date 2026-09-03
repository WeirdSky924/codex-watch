import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_goal_watchdog.monitor import MONITOR_TICK, run_monitor
from codex_goal_watchdog.recovery import RecoveryConfig, RecoveryStep
from codex_goal_watchdog.sessions import ThreadTelemetry, ThreadTelemetryTracker
from codex_goal_watchdog.tmux_control import (
    RecoveryInProgress,
    _submit_codex_text,
    _submit_shell_command,
    _submit_text,
    _submitted_text_matches,
    _submission_is_pending,
    capture_update_prompt_version,
    commands_for_step,
    ensure_codex_version,
    execute_steps,
    retry_codex_submission,
    recovery_not_before,
    recovery_phase,
    save_recovery_phase,
    session_recovery_lock,
    goal_state_from_text,
    handle_goal_prompt,
    monitor_pipe_command,
    resume_interrupted_update,
    update_prompt_version,
    wait_for_pane_state,
)


THREAD_ID = "550e8400-e29b-41d4-a716-446655440000"


class TmuxControlTests(unittest.TestCase):
    def test_recovery_phase_round_trip_uses_tmux_options(self):
        values = {
            "@codex_recovery_phase": "cooldown",
            "@codex_recovery_not_before": "1234.5",
        }
        calls = []

        def runner(command, **kwargs):
            calls.append(command)

            class Result:
                returncode = 0
                stdout = values.get(command[-1], "")

            return Result()

        self.assertEqual(
            "cooldown",
            recovery_phase("codex-goal", runner=runner),
        )
        self.assertEqual(
            1234.5,
            recovery_not_before("codex-goal", runner=runner),
        )

        save_recovery_phase(
            "codex-goal",
            "awaiting_verification",
            not_before=9.0,
            reason="503",
            runner=runner,
        )

        self.assertEqual(
            [
                ["tmux", "show-option", "-v", "-t", "codex-goal", "@codex_recovery_phase"],
                ["tmux", "show-option", "-v", "-t", "codex-goal", "@codex_recovery_not_before"],
                ["tmux", "set-option", "-t", "codex-goal", "@codex_recovery_phase", "awaiting_verification"],
                ["tmux", "set-option", "-t", "codex-goal", "@codex_recovery_not_before", "9.0"],
                ["tmux", "set-option", "-t", "codex-goal", "@codex_last_recovery_reason", "503"],
            ],
            calls,
        )
    def _telemetry(
        self,
        *,
        total_tokens=10_000,
        tokens_at_last_progress=0,
        last_event_at=100.0,
        turn_active=False,
    ):
        return ThreadTelemetry(
            thread_id=THREAD_ID,
            rollout_path=Path("/tmp/rollout.jsonl"),
            rollout_bytes=100,
            total_tokens=total_tokens,
            context_tokens=400,
            context_window=1_000,
            compaction_count=0,
            tokens_at_last_progress=tokens_at_last_progress,
            last_event_at=last_event_at,
            last_progress_at=last_event_at,
            turn_active=turn_active,
        )

    def test_submit_text_retries_when_initial_codex_composer_stays_open(self):
        calls = []
        sleeps = []
        captures = iter(("Create a plan?\n", "Working (1s)\n"))

        def runner(command, **kwargs):
            calls.append(command)

            class Result:
                returncode = 0
                stdout = next(captures) if command[1] == "capture-pane" else ""

            return Result()

        _submit_text(
            "codex-goal",
            "handoff prompt",
            runner=runner,
            sleeper=sleeps.append,
        )

        enter_calls = [
            command
            for command in calls
            if command[:5] == ["tmux", "send-keys", "-t", "codex-goal", "Enter"]
        ]
        self.assertEqual(2, len(enter_calls))
        self.assertEqual(2, len(sleeps))
        self.assertEqual(2, len([c for c in calls if c[1] == "capture-pane"]))

    def test_submit_text_retries_when_the_actual_text_stays_in_composer(self):
        calls = []
        sleeps = []
        captures = iter(
            (
                "› handoff prompt\n",
                "› handoff prompt\n",
                "› Ask Codex to do anything\n",
            )
        )

        def runner(command, **kwargs):
            calls.append(command)

            class Result:
                returncode = 0
                stdout = next(captures) if command[1] == "capture-pane" else ""

            return Result()

        _submit_text(
            "codex-goal",
            "handoff prompt",
            runner=runner,
            sleeper=sleeps.append,
        )

        enter_calls = [
            command
            for command in calls
            if command[:5] == ["tmux", "send-keys", "-t", "codex-goal", "Enter"]
        ]
        self.assertEqual(3, len(enter_calls))
        self.assertEqual(3, len(sleeps))

    def test_submission_pending_ignores_text_in_transcript_history(self):
        self.assertFalse(
            _submission_is_pending(
                "› handoff prompt\n"
                "Working (1s)\n"
                "› Ask Codex to do anything\n",
                "handoff prompt",
            )
        )

    def test_submission_pending_ignores_old_plan_picker_in_history(self):
        self.assertFalse(
            _submission_is_pending(
                "› old prompt\n"
                "Create a plan?\n"
                "› Ask Codex to do anything\n",
                "new prompt",
            )
        )

    def test_submit_text_retries_pending_shell_input(self):
        calls = []
        sleeps = []
        captures = iter(
            (
                "bash-5.2# codex resume abc\n",
                "bash-5.2#\n",
            )
        )
        pane_commands = iter(("bash",))

        def runner(command, **kwargs):
            calls.append(command)

            class Result:
                returncode = 0
                stdout = (
                    next(captures)
                    if command[1] == "capture-pane"
                    else (
                        next(pane_commands)
                        if command[1] == "display-message"
                        else ""
                    )
                )

            return Result()

        _submit_text(
            "codex-goal",
            "codex resume abc",
            runner=runner,
            sleeper=sleeps.append,
        )

        enter_calls = [
            command
            for command in calls
            if command[:5] == ["tmux", "send-keys", "-t", "codex-goal", "Enter"]
        ]
        self.assertEqual(2, len(enter_calls))
        self.assertEqual(2, len(sleeps))

    def test_submit_text_does_not_retry_shell_command_already_running(self):
        calls = []
        sleeps = []

        def runner(command, **kwargs):
            calls.append(command)

            class Result:
                returncode = 0
                stdout = (
                    "bash-5.2# codex resume abc\n"
                    if command[1] == "capture-pane"
                    else "node\n"
                    if command[1] == "display-message"
                    else ""
                )

            return Result()

        _submit_text(
            "codex-goal",
            "codex resume abc",
            runner=runner,
            sleeper=sleeps.append,
        )

        enter_calls = [
            command
            for command in calls
            if command[:5] == ["tmux", "send-keys", "-t", "codex-goal", "Enter"]
        ]
        self.assertEqual(1, len(enter_calls))
        self.assertEqual(1, len(sleeps))

    def test_submitted_text_match_ignores_soft_wrap_spaces(self):
        expected = (
            "上一 Codex thread 因 watchdog thread-health 阈值触发，"
            "请创建一个不设置 token budget 的新 Goal。"
        )
        wrapped = (
            "› 上一 Codex thread 因 watchdog thread-health 阈值触发，"
            "请创建一个不设 置 token budget 的新 Goal。"
        )

        self.assertTrue(_submitted_text_matches(wrapped, expected))

    def test_submit_text_raises_when_composer_remains_after_retries(self):
        captures = iter(["› handoff prompt\n"] * 12)

        def runner(command, **kwargs):
            class Result:
                returncode = 0
                stdout = next(captures) if command[1] == "capture-pane" else ""

            return Result()

        with self.assertRaisesRegex(TimeoutError, "remained in composer"):
            _submit_text(
                "codex-goal",
                "handoff prompt",
                runner=runner,
                sleeper=lambda seconds: None,
            )

    def test_retry_codex_submission_accepts_wrapped_composer(self):
        calls = []
        captures = iter(
            (
                "› 上一 Codex thread 因 watchdog thread-health 阈值触发，"
                "请创建一个不设 置 token budget 的新 Goal。\n",
                "› Ask Codex to do anything\n",
            )
        )

        def runner(command, **kwargs):
            calls.append(command)

            class Result:
                returncode = 0
                stdout = next(captures) if command[1] == "capture-pane" else ""

            return Result()

        self.assertTrue(
            retry_codex_submission(
                "codex-goal",
                expected="上一 Codex thread 因 watchdog thread-health 阈值触发，"
                "请创建一个不设置 token budget 的新 Goal。",
                runner=runner,
                sleeper=lambda seconds: None,
            )
        )
        self.assertEqual(
            [
                ["tmux", "capture-pane", "-p", "-J", "-t", "codex-goal"],
                ["tmux", "send-keys", "-t", "codex-goal", "Enter"],
                ["tmux", "capture-pane", "-p", "-J", "-t", "codex-goal"],
            ],
            calls,
        )

    def test_session_recovery_lock_is_nonblocking(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "session.lock"
            with session_recovery_lock("codex-goal", lock_path=lock_path):
                with self.assertRaises(RecoveryInProgress):
                    with session_recovery_lock("codex-goal", lock_path=lock_path):
                        pass

    @patch("codex_goal_watchdog.tmux_control._current_pane_command", return_value="bash")
    def test_submit_codex_text_refuses_to_inject_prompt_into_shell(self, _command_mock):
        calls = []

        with self.assertRaisesRegex(TimeoutError, "Codex text"):
            _submit_codex_text(
                "codex-goal",
                '继续执行，检查 "当前状态"。',
                runner=lambda command, **kwargs: calls.append(command),
                sleeper=lambda seconds: None,
            )

        self.assertEqual([], calls)

    @patch("codex_goal_watchdog.tmux_control._current_pane_command", return_value="bash")
    def test_submit_codex_quit_is_skipped_when_codex_already_exited(self, _command_mock):
        calls = []

        _submit_codex_text(
            "codex-goal",
            "/quit",
            runner=lambda command, **kwargs: calls.append(command),
            sleeper=lambda seconds: None,
        )

        self.assertEqual([], calls)

    @patch("codex_goal_watchdog.tmux_control._current_pane_command", return_value="bash")
    def test_submit_shell_command_cancels_bash_continuation_before_launch(self, _command_mock):
        calls = []
        sleeps = []

        _submit_shell_command(
            "codex-goal",
            "env codex resume abc",
            runner=lambda command, **kwargs: calls.append(command),
            sleeper=sleeps.append,
        )

        self.assertEqual(
            [
                ["tmux", "send-keys", "-t", "codex-goal", "C-c"],
                ["tmux", "send-keys", "-t", "codex-goal", "-l", "env codex resume abc"],
                ["tmux", "send-keys", "-t", "codex-goal", "Enter"],
            ],
            calls,
        )
        self.assertEqual([0.5], sleeps)

    def test_active_turn_is_not_rotated_for_no_progress_tokens(self):
        calls = []
        telemetry = self._telemetry(turn_active=True)

        run_monitor(
            lines=["Pursuing goal (4m)\n", MONITOR_TICK],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID, thread_no_progress_tokens=500),
            resolve_thread_telemetry=lambda thread_id: telemetry,
            now=lambda: 101.0,
            execute=lambda target, steps: calls.append((target, steps)),
            log=lambda message: None,
        )

        self.assertEqual([], calls)

    def test_recent_turn_end_is_not_rotated_before_health_poll_interval(self):
        calls = []
        telemetry = self._telemetry()

        run_monitor(
            lines=["Pursuing goal (4m)\n", MONITOR_TICK],
            target="codex-goal",
            config=RecoveryConfig(
                thread_id=THREAD_ID,
                thread_no_progress_tokens=500,
                thread_health_poll_seconds=30,
            ),
            resolve_thread_telemetry=lambda thread_id: telemetry,
            now=lambda: 101.0,
            execute=lambda target, steps: calls.append((target, steps)),
            log=lambda message: None,
        )

        self.assertEqual([], calls)

    def test_active_turn_does_not_resume_stale_paused_goal_marker(self):
        resumed = []
        telemetry = self._telemetry(total_tokens=100, tokens_at_last_progress=100, turn_active=True)

        run_monitor(
            lines=["Goal paused (/goal resume)\n", "Working (5s)\n"],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID),
            resolve_thread_telemetry=lambda thread_id: telemetry,
            resume_goal=resumed.append,
            now=lambda: 101.0,
            log=lambda message: None,
        )

        self.assertEqual([], resumed)

    def test_rollout_tracker_tracks_active_turn_lifecycle(self):
        events = [
            {
                "timestamp": "2026-08-27T08:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": THREAD_ID,
                    "cwd": "/workspace/target",
                    "source": "cli",
                    "timestamp": "2026-08-27T08:00:00Z",
                },
            },
            {
                "timestamp": "2026-08-27T08:01:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started"},
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / f"rollout-{THREAD_ID}.jsonl"
            path.write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )
            tracker = ThreadTelemetryTracker(
                thread_id=THREAD_ID,
                sessions_root=Path(temp_dir),
            )
            active = tracker.snapshot()
            with path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "timestamp": "2026-08-27T08:02:00Z",
                            "type": "event_msg",
                            "payload": {"type": "turn_aborted"},
                        }
                    )
                    + "\n"
                )
            inactive = tracker.snapshot()

        self.assertIsNotNone(active)
        self.assertIsNotNone(inactive)
        assert active is not None
        assert inactive is not None
        self.assertTrue(active.turn_active)
        self.assertFalse(inactive.turn_active)

    def test_goal_active_objective_survives_fatal_shell_exit(self):
        screen = (
            "■ unexpected status 502 Bad Gateway: Upstream access denied\n"
            "• Goal active Objective: Goal ID: FE-CREATOR-8\n"
            "To continue this session, run codex resume old-thread\n"
        )

        self.assertEqual("pursuing", goal_state_from_text(screen))

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

    def test_capture_update_prompt_version_allows_prior_conversation(self):
        def runner(command, **kwargs):
            class Result:
                returncode = 0
                stdout = (
                    "• Finished checking the previous task.\n"
                    "\n"
                    "Update available! 0.144.6 -> 0.145.0\n"
                    "1. Update now (runs `npm install -g @openai/codex`)\n"
                    "2. Skip\n3. Skip until next version\n"
                )

            return Result()

        self.assertEqual(
            "0.145.0",
            capture_update_prompt_version("codex-goal", runner=runner),
        )

    def test_capture_update_prompt_version_ignores_historical_picker(self):
        picker = (
            "Update available! 0.144.6 -> 0.145.0\n"
            "1. Update now (runs `npm install -g @openai/codex`)\n"
            "2. Skip\n3. Skip until next version\n"
        )
        trailing_states = (
            "\n› Ask Codex to do anything\n",
            "\n• Goal active Objective: continue the project\n",
            "\n(base) root@host:/workspace# codex resume thread-id\n",
        )

        for trailing_state in trailing_states:
            with self.subTest(trailing_state=trailing_state):
                def runner(command, **kwargs):
                    class Result:
                        returncode = 0
                        stdout = picker + trailing_state

                    return Result()

                self.assertIsNone(
                    capture_update_prompt_version("codex-goal", runner=runner)
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
                [
                    "tmux",
                    "display-message",
                    "-p",
                    "-t",
                    "codex-goal",
                    "#{pane_current_command}",
                ],
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

    def test_handle_goal_prompt_stops_immediately_for_achieved_goal(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)

            class Result:
                stdout = "Goal achieved (21h 55m)"

            return Result()

        handled = handle_goal_prompt(
            "codex-goal",
            action="resume",
            prompt="继续旧 goal",
            timeout_seconds=0,
            runner=runner,
        )

        self.assertTrue(handled)
        self.assertEqual(
            [["tmux", "capture-pane", "-p", "-t", "codex-goal"]],
            calls,
        )

    def test_handle_goal_prompt_uses_latest_goal_state_from_screen_history(self):
        calls = []
        sleeps = []

        def runner(command, **kwargs):
            calls.append(command)

            class Result:
                stdout = (
                    "Goal blocked (/goal resume)\n"
                    "Goal paused (/goal resume)\n"
                )

            return Result()

        handled = handle_goal_prompt(
            "codex-goal",
            action="resume",
            prompt="继续 goal",
            runner=runner,
            sleeper=sleeps.append,
        )

        self.assertTrue(handled)
        self.assertEqual([0.5], sleeps)
        self.assertEqual(
            [
                ["tmux", "capture-pane", "-p", "-t", "codex-goal"],
                [
                    "tmux",
                    "display-message",
                    "-p",
                    "-t",
                    "codex-goal",
                    "#{pane_current_command}",
                ],
                ["tmux", "send-keys", "-t", "codex-goal", "-l", "/goal resume"],
                ["tmux", "send-keys", "-t", "codex-goal", "Enter"],
            ],
            calls,
        )

    def test_handle_goal_prompt_resumes_paused_goal(self):
        calls = []
        sleeps = []

        def runner(command, **kwargs):
            calls.append(command)

            class Result:
                stdout = "Goal paused (/goal resume)"

            return Result()

        handled = handle_goal_prompt(
            "codex-goal",
            action="resume",
            prompt="继续 goal",
            runner=runner,
            sleeper=sleeps.append,
        )

        self.assertTrue(handled)
        self.assertEqual([0.5], sleeps)
        self.assertEqual(
            [
                ["tmux", "capture-pane", "-p", "-t", "codex-goal"],
                [
                    "tmux",
                    "display-message",
                    "-p",
                    "-t",
                    "codex-goal",
                    "#{pane_current_command}",
                ],
                ["tmux", "send-keys", "-t", "codex-goal", "-l", "/goal resume"],
                ["tmux", "send-keys", "-t", "codex-goal", "Enter"],
            ],
            calls,
        )

    def test_handle_goal_prompt_leaves_blocked_goal_for_manual_review(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)

            class Result:
                stdout = "Goal blocked (/goal resume)"

            return Result()

        handled = handle_goal_prompt(
            "codex-goal",
            action="resume",
            prompt="继续 goal",
            runner=runner,
        )

        self.assertTrue(handled)
        self.assertEqual(
            [["tmux", "capture-pane", "-p", "-t", "codex-goal"]],
            calls,
        )

    def test_handle_goal_prompt_leaves_stalled_goal_for_manual_resume(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)

            class Result:
                stdout = "Goal stalled (/goal resume)"

            return Result()

        handled = handle_goal_prompt(
            "codex-goal",
            action="resume",
            prompt="继续 goal",
            runner=runner,
        )

        self.assertTrue(handled)
        self.assertEqual(
            [["tmux", "capture-pane", "-p", "-t", "codex-goal"]],
            calls,
        )

    def test_handle_goal_prompt_resumes_stalled_goal_after_fatal_recovery(self):
        calls = []
        sleeps = []

        def runner(command, **kwargs):
            calls.append(command)

            class Result:
                stdout = "Goal stalled (/goal resume)"

            return Result()

        handled = handle_goal_prompt(
            "codex-goal",
            action="resume_stalled",
            prompt="继续 goal",
            runner=runner,
            sleeper=sleeps.append,
        )

        self.assertTrue(handled)
        self.assertEqual([0.5], sleeps)
        self.assertEqual(
            [
                ["tmux", "capture-pane", "-p", "-t", "codex-goal"],
                [
                    "tmux",
                    "display-message",
                    "-p",
                    "-t",
                    "codex-goal",
                    "#{pane_current_command}",
                ],
                ["tmux", "send-keys", "-t", "codex-goal", "-l", "/goal resume"],
                ["tmux", "send-keys", "-t", "codex-goal", "Enter"],
            ],
            calls,
        )

    def test_handle_goal_prompt_does_not_accept_picker_for_blocked_goal(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)

            class Result:
                stdout = (
                    "Goal blocked (/goal resume)\n"
                    "Resume paused goal?\n1. Resume goal\n2. Leave paused\n"
                )

            return Result()

        handled = handle_goal_prompt(
            "codex-goal",
            action="resume",
            prompt="继续 goal",
            runner=runner,
        )

        self.assertTrue(handled)
        self.assertEqual(
            [["tmux", "capture-pane", "-p", "-t", "codex-goal"]],
            calls,
        )

    @patch("codex_goal_watchdog.tmux_control.clear_pending_update_version")
    @patch("codex_goal_watchdog.tmux_control.execute_steps")
    @patch(
        "codex_goal_watchdog.tmux_control.recovery_goal_state_on_screen",
        return_value="pursuing",
    )
    @patch("codex_goal_watchdog.tmux_control.pane_codex_running", return_value=False)
    @patch("codex_goal_watchdog.tmux_control.pane_shell_ready", return_value=True)
    @patch(
        "codex_goal_watchdog.tmux_control.pending_update_version",
        return_value="0.145.0",
    )
    def test_interrupted_update_finishes_from_shell_before_restarting_thread(
        self,
        _pending_update_mock,
        _pane_shell_ready_mock,
        _pane_codex_running_mock,
        _goal_state_mock,
        execute_steps_mock,
        clear_pending_update_mock,
    ):
        config = RecoveryConfig(thread_id=THREAD_ID, cooldown_seconds=300)

        resume_interrupted_update("codex-goal", config)

        steps = execute_steps_mock.call_args.args[1]
        self.assertEqual(RecoveryStep("wait_shell", "300"), steps[0])
        self.assertEqual(
            RecoveryStep("ensure_codex_version", "0.145.0"), steps[1]
        )
        self.assertEqual("shell_command", steps[2].kind)
        self.assertIn(THREAD_ID, steps[2].value)
        self.assertNotIn(RecoveryStep("sleep", "300"), steps)
        clear_pending_update_mock.assert_called_once_with("codex-goal")

    @patch("codex_goal_watchdog.tmux_control.clear_pending_update_version")
    @patch(
        "codex_goal_watchdog.tmux_control.execute_steps",
        side_effect=RuntimeError("version verification failed"),
    )
    @patch("codex_goal_watchdog.tmux_control.pane_codex_running", return_value=False)
    @patch("codex_goal_watchdog.tmux_control.pane_shell_ready", return_value=True)
    @patch(
        "codex_goal_watchdog.tmux_control.pending_update_version",
        return_value="0.145.0",
    )
    def test_interrupted_update_keeps_pending_marker_when_verification_fails(
        self,
        _pending_update_mock,
        _pane_shell_ready_mock,
        _pane_codex_running_mock,
        _execute_steps_mock,
        clear_pending_update_mock,
    ):
        with self.assertRaisesRegex(RuntimeError, "version verification failed"):
            resume_interrupted_update(
                "codex-goal",
                RecoveryConfig(thread_id=THREAD_ID),
            )

        clear_pending_update_mock.assert_not_called()

    @patch("codex_goal_watchdog.tmux_control.clear_pending_update_version")
    @patch("codex_goal_watchdog.tmux_control.execute_steps")
    @patch(
        "codex_goal_watchdog.tmux_control.recovery_goal_state_on_screen",
        return_value="achieved",
    )
    @patch("codex_goal_watchdog.tmux_control.pane_shell_ready", return_value=True)
    @patch(
        "codex_goal_watchdog.tmux_control.pending_update_version",
        return_value="0.145.0",
    )
    def test_interrupted_update_does_not_recover_achieved_goal(
        self,
        _pending_update_mock,
        _pane_shell_ready_mock,
        _goal_state_mock,
        execute_steps_mock,
        _clear_pending_update_mock,
    ):
        resume_interrupted_update(
            "codex-goal",
            RecoveryConfig(thread_id=THREAD_ID),
        )

        steps = execute_steps_mock.call_args.args[1]
        self.assertFalse(
            {"leave_goal_paused", "resume_goal_or_prompt"}
            & {step.kind for step in steps}
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

    def test_execute_steps_waits_for_text_to_settle_before_enter(self):
        events = []

        execute_steps(
            "codex-goal",
            [RecoveryStep("text", "/quit")],
            runner=lambda command, **kwargs: events.append(("run", command)),
            sleeper=lambda seconds: events.append(("sleep", seconds)),
        )

        self.assertEqual(
            [
                (
                    "run",
                    [
                        "tmux",
                        "display-message",
                        "-p",
                        "-t",
                        "codex-goal",
                        "#{pane_current_command}",
                    ],
                ),
                (
                    "run",
                    ["tmux", "send-keys", "-t", "codex-goal", "-l", "/quit"],
                ),
                ("sleep", 0.5),
                (
                    "run",
                    ["tmux", "send-keys", "-t", "codex-goal", "Enter"],
                ),
            ],
            events,
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

    def test_monitor_pipe_command_omits_legacy_compaction_threshold(self):
        command = monitor_pipe_command(
            root_dir="/opt/codex-goal-watchdog",
            session="codex-goal",
            thread_id="550e8400-e29b-41d4-a716-446655440000",
            primary_model="gpt-5.6-sol",
            primary_reasoning_effort="max",
            compact_model="gpt-5.6-luna",
            compact_reasoning_effort="xhigh",
            codex_args=[],
            resume_prompt="continue",
            log_path="/tmp/watchdog.log",
            thread_max_compactions=3,
            thread_max_rollout_bytes=1234,
            thread_max_context_tokens=5678,
            thread_no_progress_tokens=9012,
            thread_no_event_seconds=3456,
            thread_health_poll_seconds=78,
            thread_max_repeated_content=4,
            thread_max_repeated_commands=5,
        )

        self.assertNotIn("--thread-max-compactions", command)
        self.assertIn("--thread-max-rollout-bytes 1234", command)
        self.assertIn("--thread-max-context-tokens 5678", command)
        self.assertIn("--thread-no-progress-tokens 9012", command)
        self.assertIn("--thread-no-event-seconds 3456", command)
        self.assertIn("--thread-health-poll-seconds 78", command)
        self.assertIn("--thread-max-repeated-content 4", command)
        self.assertIn("--thread-max-repeated-commands 5", command)


if __name__ == "__main__":
    unittest.main()
