import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from codex_goal_watchdog import __version__
from codex_goal_watchdog.__main__ import guardian_main, start_main


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


if __name__ == "__main__":
    unittest.main()
