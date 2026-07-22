import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from codex_goal_watchdog import __version__
from codex_goal_watchdog.__main__ import guardian_main, main, start_main


class ConsoleEntrypointTests(unittest.TestCase):
    @patch("codex_goal_watchdog.__main__.main", return_value=7)
    def test_codex_watch_injects_start_subcommand(self, main_mock):
        with patch.object(sys, "argv", ["codex-watch", "--resume", "--safe"]):
            result = start_main()

        self.assertEqual(7, result)
        main_mock.assert_called_once_with(["start", "--resume", "--safe"])

    @patch("codex_goal_watchdog.__main__.main", return_value=9)
    def test_guardian_entrypoint_injects_guardian_subcommand(self, main_mock):
        with patch.object(
            sys,
            "argv",
            ["codex-watch-guardian", "--session", "backend"],
        ):
            result = guardian_main()

        self.assertEqual(9, result)
        main_mock.assert_called_once_with(["guardian", "--session", "backend"])

    def test_console_entrypoints_report_package_version(self):
        for command in ("codex-watch", "codex-watch-guardian"):
            with self.subTest(command=command):
                entrypoint = start_main if command == "codex-watch" else guardian_main
                output = StringIO()
                with patch.object(sys, "argv", [command, "--version"]):
                    with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                        entrypoint()

                self.assertEqual(0, raised.exception.code)
                self.assertEqual(f"{command} {__version__}", output.getvalue().strip())

    @patch("codex_goal_watchdog.__main__.tmux_get_thread_id", return_value=None)
    @patch("codex_goal_watchdog.__main__.tmux_session_exists", return_value=True)
    def test_existing_unmanaged_tmux_explains_how_to_connect_codex_session(
        self,
        _session_exists_mock,
        _get_thread_id_mock,
    ):
        with self.assertRaises(SystemExit) as raised:
            main(["start", "--session", "existing-session"])

        message = str(raised.exception)
        self.assertIn("not initialized by codex-watch", message)
        self.assertIn("create or resume a Codex conversation", message)
        self.assertIn("thread UUID", message)
        self.assertIn(
            "codex-watch --session existing-session --thread-id <UUID>",
            message,
        )
        self.assertIn("unused tmux session name", message)
        self.assertIn("codex-watch --session <NEW_SESSION>", message)

    @patch("codex_goal_watchdog.__main__.handle_goal_prompt")
    @patch("codex_goal_watchdog.__main__.subprocess.run")
    @patch(
        "codex_goal_watchdog.__main__.tmux_get_thread_id",
        return_value="550e8400-e29b-41d4-a716-446655440000",
    )
    @patch("codex_goal_watchdog.__main__.tmux_session_exists", return_value=True)
    def test_manual_attach_resumes_visible_paused_goal(
        self,
        _session_exists_mock,
        _get_thread_id_mock,
        _run_mock,
        handle_goal_prompt_mock,
    ):
        result = main(["start", "--session", "existing-session", "--no-attach"])

        self.assertEqual(0, result)
        handle_goal_prompt_mock.assert_called_once_with(
            "existing-session",
            action="resume",
            prompt="",
            timeout_seconds=0,
            send_fallback_prompt=False,
        )

    @patch("codex_goal_watchdog.__main__.handle_goal_prompt")
    @patch("codex_goal_watchdog.__main__.execute_steps")
    @patch(
        "codex_goal_watchdog.__main__.capture_update_prompt_version",
        return_value="0.145.0",
    )
    @patch("codex_goal_watchdog.__main__.wait_for_new_thread_id")
    @patch("codex_goal_watchdog.__main__.subprocess.run")
    @patch("codex_goal_watchdog.__main__.tmux_session_exists", return_value=False)
    def test_fresh_start_handles_update_before_waiting_for_thread_id(
        self,
        _session_exists_mock,
        _run_mock,
        wait_for_thread_mock,
        _capture_update_mock,
        execute_steps_mock,
        _handle_goal_prompt_mock,
    ):
        thread_id = "550e8400-e29b-41d4-a716-446655440000"

        def wait_for_thread(**kwargs):
            self.assertTrue(kwargs["on_wait"]())
            return thread_id

        wait_for_thread_mock.side_effect = wait_for_thread

        with redirect_stdout(StringIO()):
            result = main(["start", "--session", "fresh-session", "--no-attach"])

        self.assertEqual(0, result)
        update_steps = execute_steps_mock.call_args.args[1]
        self.assertEqual("1", update_steps[0].value)
        self.assertEqual("ensure_codex_version", update_steps[2].kind)
        self.assertEqual("0.145.0", update_steps[2].value)


if __name__ == "__main__":
    unittest.main()
