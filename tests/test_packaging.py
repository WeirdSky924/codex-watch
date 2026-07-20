import re
import tomllib
import unittest
from pathlib import Path

from codex_goal_watchdog import __version__


ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_pyproject_declares_package_and_console_scripts(self):
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual("codex-goal-watchdog", metadata["project"]["name"])
        self.assertEqual(__version__, metadata["project"]["version"])
        self.assertEqual(
            "codex_goal_watchdog.__main__:start_main",
            metadata["project"]["scripts"]["codex-watch"],
        )
        self.assertEqual(
            "codex_goal_watchdog.__main__:guardian_main",
            metadata["project"]["scripts"]["codex-watch-guardian"],
        )

    def test_distribution_includes_release_and_install_assets(self):
        for relative_path in (
            "LICENSE",
            "CHANGELOG.md",
            "MANIFEST.in",
            "install.sh",
            "uninstall.sh",
            "systemd/codex-watch-guardian@.service",
            ".github/workflows/tests.yml",
        ):
            with self.subTest(path=relative_path):
                self.assertTrue((ROOT / relative_path).is_file())

    def test_user_service_uses_user_local_console_script(self):
        service = (ROOT / "systemd/codex-watch-guardian@.service").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin",
            service,
        )
        self.assertIn(
            "ExecStart=/usr/bin/env codex-watch-guardian --session %i",
            service,
        )
        self.assertIn("WantedBy=default.target", service)
        self.assertNotIn("ExecStart=/usr/local/bin", service)

    def test_public_readme_has_no_machine_specific_identifiers(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertNotIn("/home/", readme)
        self.assertNotIn("/root/", readme)
        self.assertIsNone(
            re.search(
                r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                r"[0-9a-f]{4}-[0-9a-f]{12}\b",
                readme,
                re.IGNORECASE,
            )
        )
        self.assertIn("$XDG_STATE_HOME/codex-goal-watchdog/watchdog.log", readme)


if __name__ == "__main__":
    unittest.main()
