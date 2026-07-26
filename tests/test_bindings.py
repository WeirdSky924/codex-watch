import tempfile
import unittest
from pathlib import Path

from codex_goal_watchdog.bindings import (
    load_session_binding,
    save_session_binding,
)


class SessionBindingTests(unittest.TestCase):
    def test_save_and_load_binding_by_watchdog_session(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            state_root = Path(temporary_dir)
            save_session_binding(
                session="project-a",
                thread_id="550e8400-e29b-41d4-a716-446655440000",
                cwd=Path("/workspace/project-a"),
                state_root=state_root,
            )

            binding = load_session_binding(
                "project-a",
                state_root=state_root,
            )

            self.assertIsNotNone(binding)
            assert binding is not None
            self.assertEqual("project-a", binding.session)
            self.assertEqual(
                "550e8400-e29b-41d4-a716-446655440000",
                binding.thread_id,
            )
            self.assertEqual(Path("/workspace/project-a"), binding.cwd)
            binding_files = list((state_root / "bindings").glob("*.json"))
            self.assertEqual(1, len(binding_files))
            self.assertEqual(0o600, binding_files[0].stat().st_mode & 0o777)

    def test_bindings_are_isolated_by_watchdog_session(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            state_root = Path(temporary_dir)
            save_session_binding(
                session="project-a",
                thread_id="550e8400-e29b-41d4-a716-446655440000",
                cwd=Path("/workspace/project-a"),
                state_root=state_root,
            )
            save_session_binding(
                session="project-b",
                thread_id="550e8400-e29b-41d4-a716-446655440001",
                cwd=Path("/workspace/project-b"),
                state_root=state_root,
            )

            project_a = load_session_binding("project-a", state_root=state_root)
            project_b = load_session_binding("project-b", state_root=state_root)

            self.assertEqual(
                "550e8400-e29b-41d4-a716-446655440000",
                project_a.thread_id if project_a else None,
            )
            self.assertEqual(
                "550e8400-e29b-41d4-a716-446655440001",
                project_b.thread_id if project_b else None,
            )


if __name__ == "__main__":
    unittest.main()
