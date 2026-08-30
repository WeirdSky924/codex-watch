import unittest
from pathlib import Path

from codex_goal_watchdog.launcher import (
    build_codex_command,
    normalize_codex_args,
    tmux_new_session_command,
    tmux_pane_identity,
    tmux_pipe_pane_command,
)


class LauncherTests(unittest.TestCase):
    def test_normalize_codex_args_enables_dangerous_bypass_by_default(self):
        self.assertEqual(
            ["--dangerously-bypass-approvals-and-sandbox"],
            normalize_codex_args([], safe_mode=False),
        )

    def test_normalize_codex_args_can_disable_default_bypass(self):
        self.assertEqual([], normalize_codex_args([], safe_mode=True))

    def test_normalize_codex_args_does_not_duplicate_bypass(self):
        flag = "--dangerously-bypass-approvals-and-sandbox"

        self.assertEqual(
            [flag, "--search"],
            normalize_codex_args([flag, "--search"], safe_mode=False),
        )

    def test_build_codex_command_adds_model_and_reasoning_effort(self):
        command = build_codex_command(
            model="gpt-5.6-sol",
            reasoning_effort="max",
            codex_args=["--dangerously-bypass-approvals-and-sandbox"],
        )

        self.assertEqual(
            [
                "env",
                "-u",
                "NO_COLOR",
                "-u",
                "CODEX_THREAD_ID",
                "-u",
                "CODEX_CI",
                "-u",
                "CODEX_SESSION_ID",
                "COLORTERM=truecolor",
                "codex",
                "--no-alt-screen",
                "-m",
                "gpt-5.6-sol",
                "-c",
                'model_reasoning_effort="max"',
                "--dangerously-bypass-approvals-and-sandbox",
            ],
            command,
        )

    def test_build_codex_command_unsets_inherited_codex_session_identity(self):
        command = build_codex_command(
            model="gpt-5.6-sol",
            reasoning_effort="max",
        )

        unset_names = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == "-u"
        ]
        self.assertEqual(
            ["NO_COLOR", "CODEX_THREAD_ID", "CODEX_CI", "CODEX_SESSION_ID"],
            unset_names,
        )

    def test_build_codex_command_can_resume_last_session(self):
        command = build_codex_command(
            model="gpt-5.6-luna",
            reasoning_effort="xhigh",
            codex_args=["--dangerously-bypass-approvals-and-sandbox"],
            resume_last=True,
        )

        self.assertEqual(
            [
                "env",
                "-u",
                "NO_COLOR",
                "-u",
                "CODEX_THREAD_ID",
                "-u",
                "CODEX_CI",
                "-u",
                "CODEX_SESSION_ID",
                "COLORTERM=truecolor",
                "codex",
                "--no-alt-screen",
                "-m",
                "gpt-5.6-luna",
                "-c",
                'model_reasoning_effort="xhigh"',
                "--dangerously-bypass-approvals-and-sandbox",
                "resume",
                "--last",
            ],
            command,
        )

    def test_build_codex_command_can_resume_pinned_thread(self):
        command = build_codex_command(
            model="gpt-5.6-luna",
            reasoning_effort="xhigh",
            resume_thread_id="550e8400-e29b-41d4-a716-446655440000",
        )

        self.assertEqual(
            [
                "env",
                "-u",
                "NO_COLOR",
                "-u",
                "CODEX_THREAD_ID",
                "-u",
                "CODEX_CI",
                "-u",
                "CODEX_SESSION_ID",
                "COLORTERM=truecolor",
                "codex",
                "--no-alt-screen",
                "-m",
                "gpt-5.6-luna",
                "-c",
                'model_reasoning_effort="xhigh"',
                "resume",
                "550e8400-e29b-41d4-a716-446655440000",
            ],
            command,
        )

    def test_tmux_new_session_command_uses_shell_quoted_codex_command(self):
        command = tmux_new_session_command(
            session="codex-goal",
            codex_command=["codex", "--no-alt-screen", "-m", "gpt-5.6-sol"],
        )

        self.assertEqual(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                "codex-goal",
                (
                    "codex --no-alt-screen -m gpt-5.6-sol; "
                    "exec env HISTFILE=/dev/null HISTSIZE=0 HISTFILESIZE=0 "
                    "bash --norc"
                ),
            ],
            command,
        )

    def test_tmux_pipe_pane_command_targets_session(self):
        command = tmux_pipe_pane_command(
            session="codex-goal",
            pipe_command="python3 -m codex_goal_watchdog monitor",
        )

        self.assertEqual(
            [
                "tmux",
                "pipe-pane",
                "-o",
                "-t",
                "codex-goal",
                "python3 -m codex_goal_watchdog monitor",
            ],
            command,
        )

    def test_tmux_pane_identity_parses_pid_and_working_directory(self):
        class Result:
            returncode = 0
            stdout = "1234\t/workspace/project\n"

        self.assertEqual(
            (1234, Path("/workspace/project")),
            tmux_pane_identity(
                "codex-goal",
                runner=lambda *args, **kwargs: Result(),
            ),
        )


if __name__ == "__main__":
    unittest.main()
