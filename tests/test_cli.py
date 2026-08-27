import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import call, patch

from codex_goal_watchdog import __version__
from codex_goal_watchdog.__main__ import guardian_main, main, start_main
from codex_goal_watchdog.bindings import SessionBinding


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

    @patch("codex_goal_watchdog.__main__.save_session_binding")
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
        _save_binding_mock,
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

    @patch("codex_goal_watchdog.__main__.save_session_binding")
    @patch("codex_goal_watchdog.__main__.handle_goal_prompt")
    @patch("codex_goal_watchdog.__main__.subprocess.run")
    @patch(
        "codex_goal_watchdog.__main__.tmux_get_thread_id",
        return_value="550e8400-e29b-41d4-a716-446655440000",
    )
    @patch("codex_goal_watchdog.__main__.tmux_session_exists", return_value=True)
    def test_start_reenables_installed_guardian_for_session(
        self,
        _session_exists_mock,
        _get_thread_id_mock,
        run_mock,
        _handle_goal_prompt_mock,
        _save_binding_mock,
    ):
        with tempfile.TemporaryDirectory() as home:
            unit_path = (
                Path(home)
                / ".config/systemd/user/codex-watch-guardian@.service"
            )
            unit_path.parent.mkdir(parents=True)
            unit_path.touch()

            with patch(
                "codex_goal_watchdog.__main__.Path.home",
                return_value=Path(home),
            ):
                result = main(
                    ["start", "--session", "project-a", "--no-attach"]
                )

        self.assertEqual(0, result)
        self.assertIn(
            call(
                [
                    "systemctl",
                    "--user",
                    "enable",
                    "--now",
                    "codex-watch-guardian@project-a.service",
                ],
                check=True,
            ),
            run_mock.call_args_list,
        )

    @patch("codex_goal_watchdog.__main__.save_session_binding")
    @patch("codex_goal_watchdog.__main__.handle_goal_prompt")
    @patch("codex_goal_watchdog.__main__.subprocess.run")
    @patch(
        "codex_goal_watchdog.__main__.tmux_get_thread_id",
        return_value="550e8400-e29b-41d4-a716-446655440000",
    )
    @patch("codex_goal_watchdog.__main__.tmux_session_exists", return_value=True)
    def test_start_skips_guardian_when_service_was_not_installed(
        self,
        _session_exists_mock,
        _get_thread_id_mock,
        run_mock,
        _handle_goal_prompt_mock,
        _save_binding_mock,
    ):
        with tempfile.TemporaryDirectory() as home:
            with patch(
                "codex_goal_watchdog.__main__.Path.home",
                return_value=Path(home),
            ):
                result = main(
                    ["start", "--session", "project-a", "--no-attach"]
                )

        self.assertEqual(0, result)
        systemctl_calls = [
            call
            for call in run_mock.call_args_list
            if call.args[0][0] == "systemctl"
        ]
        self.assertEqual([], systemctl_calls)

    @patch("codex_goal_watchdog.__main__.save_session_binding")
    @patch(
        "codex_goal_watchdog.__main__.load_session_binding",
        return_value=None,
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
        _load_binding_mock,
        _save_binding_mock,
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

    @patch("codex_goal_watchdog.__main__.save_session_binding")
    @patch("codex_goal_watchdog.__main__.handle_goal_prompt")
    @patch("codex_goal_watchdog.__main__.subprocess.run")
    @patch("codex_goal_watchdog.__main__.find_latest_thread_id")
    @patch("codex_goal_watchdog.__main__.tmux_session_exists", return_value=False)
    @patch("codex_goal_watchdog.__main__.load_session_binding")
    def test_default_start_resumes_thread_pinned_to_watchdog_session(
        self,
        load_binding_mock,
        _session_exists_mock,
        find_latest_mock,
        run_mock,
        _handle_goal_prompt_mock,
        save_binding_mock,
    ):
        thread_id = "550e8400-e29b-41d4-a716-446655440000"
        load_binding_mock.return_value = SessionBinding(
            session="project-a",
            thread_id=thread_id,
            cwd=Path.cwd().resolve(),
        )

        result = main(["start", "--session", "project-a", "--no-attach"])

        self.assertEqual(0, result)
        find_latest_mock.assert_not_called()
        new_session_command = next(
            call.args[0]
            for call in run_mock.call_args_list
            if call.args[0][0:2] == ["tmux", "new-session"]
        )
        self.assertIn(f"resume {thread_id}", new_session_command[-1])
        save_binding_mock.assert_called_with(
            session="project-a",
            thread_id=thread_id,
            cwd=Path.cwd().resolve(),
        )

    @patch("codex_goal_watchdog.__main__.subprocess.run")
    @patch("codex_goal_watchdog.__main__.tmux_session_exists", return_value=False)
    @patch("codex_goal_watchdog.__main__.load_session_binding")
    def test_default_start_rejects_binding_from_another_directory(
        self,
        load_binding_mock,
        _session_exists_mock,
        run_mock,
    ):
        load_binding_mock.return_value = SessionBinding(
            session="project-a",
            thread_id="550e8400-e29b-41d4-a716-446655440000",
            cwd=Path("/workspace/other"),
        )

        with self.assertRaises(SystemExit) as raised:
            main(["start", "--session", "project-a", "--no-attach"])

        self.assertIn("is bound to /workspace/other", str(raised.exception))
        self.assertIn("--new", str(raised.exception))
        run_mock.assert_not_called()

    @patch("codex_goal_watchdog.__main__.save_session_binding")
    @patch("codex_goal_watchdog.__main__.handle_goal_prompt")
    @patch("codex_goal_watchdog.__main__.subprocess.run")
    @patch(
        "codex_goal_watchdog.__main__.find_latest_thread_id",
        return_value="550e8400-e29b-41d4-a716-446655440001",
    )
    @patch("codex_goal_watchdog.__main__.tmux_session_exists", return_value=False)
    @patch("codex_goal_watchdog.__main__.load_session_binding")
    def test_explicit_resume_uses_latest_directory_thread_not_session_binding(
        self,
        load_binding_mock,
        _session_exists_mock,
        _find_latest_mock,
        run_mock,
        _handle_goal_prompt_mock,
        _save_binding_mock,
    ):
        load_binding_mock.return_value = SessionBinding(
            session="project-a",
            thread_id="550e8400-e29b-41d4-a716-446655440000",
            cwd=Path.cwd().resolve(),
        )

        result = main(
            ["start", "--session", "project-a", "--resume", "--no-attach"]
        )

        self.assertEqual(0, result)
        new_session_command = next(
            call.args[0]
            for call in run_mock.call_args_list
            if call.args[0][0:2] == ["tmux", "new-session"]
        )
        self.assertIn(
            "resume 550e8400-e29b-41d4-a716-446655440001",
            new_session_command[-1],
        )

    @patch("codex_goal_watchdog.__main__.find_latest_thread_id")
    @patch("codex_goal_watchdog.__main__.load_session_binding")
    @patch("codex_goal_watchdog.__main__.tmux_session_exists", return_value=False)
    def test_new_forces_fresh_thread_without_reading_previous_binding(
        self,
        _session_exists_mock,
        load_binding_mock,
        find_latest_mock,
    ):
        output = StringIO()

        with redirect_stdout(output):
            result = main(
                [
                    "start",
                    "--session",
                    "project-a",
                    "--new",
                    "--dry-run",
                    "--no-attach",
                ]
            )

        self.assertEqual(0, result)
        load_binding_mock.assert_not_called()
        find_latest_mock.assert_not_called()
        new_session_line = next(
            line
            for line in output.getvalue().splitlines()
            if line.startswith("tmux new-session")
        )
        self.assertNotIn(" resume ", new_session_line)

    @patch("codex_goal_watchdog.__main__.save_session_binding")
    @patch("codex_goal_watchdog.__main__.handle_goal_prompt")
    @patch("codex_goal_watchdog.__main__.subprocess.run")
    @patch("codex_goal_watchdog.__main__.tmux_session_exists", return_value=False)
    @patch("codex_goal_watchdog.__main__.load_session_binding")
    def test_start_preserves_persisted_runtime_counters(
        self,
        load_binding_mock,
        _session_exists_mock,
        run_mock,
        _handle_goal_prompt_mock,
        _save_binding_mock,
    ):
        load_binding_mock.return_value = SessionBinding(
            session="project-a",
            thread_id="550e8400-e29b-41d4-a716-446655440000",
            cwd=Path.cwd().resolve(),
            recovery_count=4,
            successful_compactions=2,
        )

        result = main(["start", "--session", "project-a", "--no-attach"])

        self.assertEqual(0, result)
        option_calls = [
            call.args[0]
            for call in run_mock.call_args_list
            if call.args and call.args[0][:3] == ["tmux", "set-option", "-t"]
        ]
        self.assertIn(
            [
                "tmux",
                "set-option",
                "-t",
                "project-a",
                "@codex_recovery_count",
                "4",
            ],
            option_calls,
        )
        self.assertIn(
            [
                "tmux",
                "set-option",
                "-t",
                "project-a",
                "@codex_successful_compactions",
                "2",
            ],
            option_calls,
        )


if __name__ == "__main__":
    unittest.main()
