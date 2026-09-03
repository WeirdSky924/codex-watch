import tempfile
import unittest
from pathlib import Path

from codex_goal_watchdog.bindings import (
    load_thread_handoff,
    load_session_binding,
    save_binding_runtime_state,
    save_session_binding,
    save_thread_handoff,
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

    def test_runtime_counters_survive_binding_rewrite(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            state_root = Path(temporary_dir)
            save_session_binding(
                session="project-a",
                thread_id="550e8400-e29b-41d4-a716-446655440000",
                cwd=Path("/workspace/project-a"),
                state_root=state_root,
                recovery_count=4,
                successful_compactions=2,
                verification_pending=True,
                verification_baseline=9,
            )

            save_binding_runtime_state(
                session="project-a",
                recovery_count=5,
                successful_compactions=3,
                verification_pending=True,
                verification_baseline=10,
                state_root=state_root,
            )
            binding = load_session_binding("project-a", state_root=state_root)

        self.assertIsNotNone(binding)
        assert binding is not None
        self.assertEqual(5, binding.recovery_count)
        self.assertEqual(3, binding.successful_compactions)
        self.assertTrue(binding.verification_pending)
        self.assertEqual(10, binding.verification_baseline)

    def test_recovery_phase_survives_binding_rewrite(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            state_root = Path(temporary_dir)
            save_session_binding(
                session="project-a",
                thread_id="550e8400-e29b-41d4-a716-446655440000",
                cwd=Path("/workspace/project-a"),
                state_root=state_root,
                recovery_phase="cooldown",
                recovery_not_before=1234.5,
                last_recovery_reason="retryable_http_503",
            )
            save_binding_runtime_state(
                session="project-a",
                recovery_count=2,
                successful_compactions=0,
                state_root=state_root,
            )
            binding = load_session_binding("project-a", state_root=state_root)

        self.assertIsNotNone(binding)
        assert binding is not None
        self.assertEqual("cooldown", binding.recovery_phase)
        self.assertEqual(1234.5, binding.recovery_not_before)
        self.assertEqual("retryable_http_503", binding.last_recovery_reason)

    def test_thread_handoff_is_bounded_and_private(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            state_root = Path(temporary_dir)
            handoff = save_thread_handoff(
                session="project-a",
                thread_id="550e8400-e29b-41d4-a716-446655440000",
                cwd=Path("/workspace/project-a"),
                reason="compaction_timeout",
                goal_objective="x" * 30_000,
                telemetry={"rollout_bytes": 42},
                state_root=state_root,
            )
            payload = handoff.read_text(encoding="utf-8")
            mode = handoff.stat().st_mode & 0o777

        self.assertLess(len(payload), 22_000)
        self.assertEqual(0o600, mode)
        self.assertIn("compaction_timeout", payload)

    def test_thread_handoff_can_be_loaded_by_watchdog_session(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            state_root = Path(temporary_dir)
            expected_path = save_thread_handoff(
                session="project-a",
                thread_id="550e8400-e29b-41d4-a716-446655440000",
                cwd=Path("/workspace/project-a"),
                reason="no_rollout_events",
                goal_objective="Goal ID: FE-CREATOR-8",
                telemetry={},
                state_root=state_root,
            )

            loaded = load_thread_handoff("project-a", state_root=state_root)

        self.assertIsNotNone(loaded)
        assert loaded is not None
        path, payload = loaded
        self.assertEqual(expected_path, path)
        self.assertEqual("no_rollout_events", payload["reason"])


if __name__ == "__main__":
    unittest.main()
