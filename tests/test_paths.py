import tempfile
import unittest
from pathlib import Path

from codex_goal_watchdog.paths import default_log_path, state_dir


class PathTests(unittest.TestCase):
    def test_state_dir_prefers_explicit_override(self):
        home = Path("/home/example")

        actual = state_dir(
            env={
                "CODEX_WATCH_STATE_DIR": "/var/tmp/codex-watch-state",
                "XDG_STATE_HOME": "/var/tmp/xdg-state",
            },
            home=home,
        )

        self.assertEqual(Path("/var/tmp/codex-watch-state"), actual)

    def test_state_dir_uses_xdg_then_home_fallback(self):
        home = Path("/home/example")

        self.assertEqual(
            Path("/var/tmp/xdg-state/codex-goal-watchdog"),
            state_dir(env={"XDG_STATE_HOME": "/var/tmp/xdg-state"}, home=home),
        )
        self.assertEqual(
            home / ".local" / "state" / "codex-goal-watchdog",
            state_dir(env={}, home=home),
        )

    def test_default_log_path_creates_private_state_directory(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir) / "state"

            actual = default_log_path(
                env={"CODEX_WATCH_STATE_DIR": str(root)},
                home=Path(temporary_dir),
            )

            self.assertEqual(root / "watchdog.log", actual)
            self.assertTrue(root.is_dir())
            self.assertEqual(0o700, root.stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
