import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from codex_goal_watchdog.__main__ import main
from codex_goal_watchdog.bindings import (
    load_session_binding,
    save_binding_runtime_state,
    save_session_binding,
)
from codex_goal_watchdog.sessions import (
    find_latest_thread_execution_profile,
    thread_rollout_size,
)


THREAD = "550e8400-e29b-41d4-a716-446655440000"
OTHER_THREAD = "550e8400-e29b-41d4-a716-446655440001"


def write_rollout(root: Path, thread_id: str, events: list[dict]) -> Path:
    path = root / f"rollout-{thread_id}.jsonl"
    with path.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "type": "session_meta",
            "timestamp": "2026-09-10T00:00:00Z",
            "payload": {
                "id": thread_id,
                "cwd": str(root),
                "source": "cli",
                "timestamp": "2026-09-10T00:00:00Z",
            },
        }) + "\n")
        for event in events:
            stream.write(json.dumps(event) + "\n")
    return path


def turn(model: str, effort: str) -> dict:
    return {
        "type": "turn_context",
        "payload": {"turn_id": "turn-latest", "model": model, "effort": effort},
    }


class StartupProfileTests(unittest.TestCase):
    def test_profile_event_starting_at_launch_offset_is_not_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = write_rollout(root, THREAD, [turn("gpt-5.6-sol", "max")])
            offset = path.stat().st_size
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_settings_applied",
                        "thread_id": THREAD,
                        "thread_settings": {
                            "model": "gpt-6-astra",
                            "reasoning_effort": "xhigh",
                        },
                    },
                }) + "\n")
            profile = find_latest_thread_execution_profile(
                thread_id=THREAD, sessions_root=root, offset=offset
            )
        self.assertIsNotNone(profile)
        self.assertEqual(("gpt-6-astra", "xhigh"), (
            profile.model, profile.reasoning_effort
        ))

    def test_partial_line_at_launch_offset_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = write_rollout(root, THREAD, [])
            with path.open("a", encoding="utf-8") as stream:
                stream.write('{"type":"turn_context","payload":')
            offset = path.stat().st_size
            with path.open("a", encoding="utf-8") as stream:
                stream.write('{"model":"gpt-5.6-sol","effort":"max"}}\n')
                stream.write(json.dumps(turn("gpt-6-astra", "xhigh")) + "\n")
            profile = find_latest_thread_execution_profile(
                thread_id=THREAD, sessions_root=root, offset=offset
            )
        self.assertEqual("gpt-6-astra", profile.model)

    def test_truncated_rollout_does_not_leave_offset_beyond_eof(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = write_rollout(root, THREAD, [turn("gpt-5.6-sol", "max")])
            old_size = path.stat().st_size
            write_rollout(root, THREAD, [turn("gpt-6", "high")])
            self.assertLess(path.stat().st_size, old_size)
            profile = find_latest_thread_execution_profile(
                thread_id=THREAD, sessions_root=root, offset=old_size
            )
        self.assertEqual(("gpt-6", "high"), (
            profile.model, profile.reasoning_effort
        ))

    def test_rollout_checkpoint_stops_before_an_incomplete_record(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = write_rollout(root, THREAD, [turn("gpt-5.6-sol", "max")])
            with path.open("a", encoding="utf-8") as stream:
                stream.write('{"type":"turn_context","payload":')
            checkpoint = thread_rollout_size(
                thread_id=THREAD, sessions_root=root
            )
            data = path.read_bytes()
        self.assertEqual(b"\n", data[checkpoint - 1 : checkpoint])
        self.assertLess(checkpoint, len(data))

    def test_latest_thread_settings_override_old_turn_after_manual_model_switch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_rollout(root, THREAD, [
                turn("gpt-5.6-sol", "max"),
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_settings_applied",
                        "thread_id": THREAD,
                        "thread_settings": {
                            "model": "gpt-6-astra",
                            "reasoning_effort": "xhigh",
                        },
                    },
                },
            ])
            profile = find_latest_thread_execution_profile(
                thread_id=THREAD, sessions_root=root
            )
        self.assertEqual(("gpt-6-astra", "xhigh"), (
            profile.model, profile.reasoning_effort
        ))

    def test_profile_is_scoped_to_the_pinned_thread(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_rollout(root, THREAD, [turn("gpt-6-astra", "xhigh")])
            write_rollout(root, OTHER_THREAD, [turn("gpt-5.6-sol", "max")])
            profile = find_latest_thread_execution_profile(
                thread_id=THREAD, sessions_root=root
            )
        self.assertEqual("gpt-6-astra", profile.model)

    def test_binding_preserves_launch_options_across_runtime_and_clear_rebind(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            launch = {
                "primary_model": "gpt-6-astra",
                "primary_reasoning_effort": "xhigh",
                "compact_model": "gpt-5.6-luna",
                "compact_reasoning_effort": "high",
                "safe": True,
                "codex_args": ["--no-alt-screen"],
            }
            save_session_binding(
                session="project-a", thread_id=THREAD, cwd=root,
                state_root=root, launch_options=launch,
            )
            save_binding_runtime_state(
                session="project-a", recovery_count=2,
                successful_compactions=1, state_root=root,
            )
            save_session_binding(
                session="project-a", thread_id=OTHER_THREAD, cwd=root,
                state_root=root,
            )
            actual = load_session_binding("project-a", state_root=root)
            self.assertEqual(launch, actual.launch_options)

    def test_default_restart_restores_last_thread_profile_and_launch_options(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_rollout(root, THREAD, [turn("gpt-6-astra", "xhigh")])
            with patch("codex_goal_watchdog.bindings.state_dir", return_value=root), \
                 patch("codex_goal_watchdog.sessions.find_thread_rollout_path",
                       return_value=root / f"rollout-{THREAD}.jsonl"), \
                 patch("codex_goal_watchdog.__main__.tmux_session_exists",
                       return_value=False):
                save_session_binding(
                    session="project-a", thread_id=THREAD, cwd=Path.cwd(),
                    launch_options={
                        "primary_model": "gpt-5.6-sol",
                        "primary_reasoning_effort": "max",
                        "compact_model": "gpt-5.6-luna",
                        "compact_reasoning_effort": "high",
                        "max_recoveries": 7,
                        "safe": True,
                        "codex_args": ["--search"],
                    },
                )
                output = StringIO()
                with redirect_stdout(output):
                    result = main(["start", "--session", "project-a",
                                   "--no-attach", "--dry-run"])
        self.assertEqual(0, result)
        self.assertIn("-m gpt-6-astra", output.getvalue())
        self.assertIn('model_reasoning_effort="xhigh"', output.getvalue())
        self.assertIn("--compact-reasoning-effort high", output.getvalue())
        self.assertIn("--max-recoveries 7", output.getvalue())
        self.assertIn("--search", output.getvalue())
        self.assertNotIn(
            "--dangerously-bypass-approvals-and-sandbox", output.getvalue()
        )
        self.assertIn(f"resume {THREAD}", output.getvalue())

    def test_explicit_options_override_saved_and_rollout_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = write_rollout(root, THREAD, [turn("gpt-6-astra", "xhigh")])
            with patch("codex_goal_watchdog.bindings.state_dir", return_value=root), \
                 patch("codex_goal_watchdog.sessions.find_thread_rollout_path",
                       return_value=path), \
                 patch("codex_goal_watchdog.__main__.tmux_session_exists",
                       return_value=False):
                save_session_binding(
                    session="project-a", thread_id=THREAD, cwd=Path.cwd(),
                    launch_options={"compact_model": "gpt-5.6-luna",
                                    "max_recoveries": 4},
                )
                output = StringIO()
                with redirect_stdout(output):
                    result = main([
                        "start", "--session", "project-a", "--primary-model",
                        "gpt-5.6-sol", "--primary-reasoning-effort", "max",
                        "--compact-model", "gpt-5.5", "--max-recoveries", "2",
                        "--dry-run", "--no-attach",
                    ])
        self.assertEqual(0, result)
        self.assertIn("-m gpt-5.6-sol", output.getvalue())
        self.assertIn('model_reasoning_effort="max"', output.getvalue())
        self.assertIn("--compact-model gpt-5.5", output.getvalue())
        self.assertIn("--max-recoveries 2", output.getvalue())

    def test_console_entrypoint_detects_explicit_options_when_argv_is_implicit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = write_rollout(root, THREAD, [turn("gpt-6-astra", "xhigh")])
            with patch("codex_goal_watchdog.bindings.state_dir", return_value=root), \
                 patch("codex_goal_watchdog.sessions.find_thread_rollout_path",
                       return_value=path), \
                 patch("codex_goal_watchdog.__main__.tmux_session_exists",
                       return_value=False), \
                 patch("codex_goal_watchdog.__main__.sys.argv", [
                     "codex-watch", "start", "--session", "project-a",
                     "--primary-model", "gpt-5.6-sol",
                     "--primary-reasoning-effort", "max",
                     "--dry-run", "--no-attach",
                 ]):
                save_session_binding(
                    session="project-a", thread_id=THREAD, cwd=Path.cwd(),
                    state_root=root,
                    launch_options={"primary_model": "gpt-5.6-luna",
                                    "primary_reasoning_effort": "high"},
                )
                output = StringIO()
                with redirect_stdout(output):
                    result = main()
        self.assertEqual(0, result)
        self.assertIn("-m gpt-5.6-sol", output.getvalue())
        self.assertIn('model_reasoning_effort="max"', output.getvalue())

    def test_saved_explicit_model_beats_old_rollout_when_no_new_event(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = write_rollout(root, THREAD, [turn("gpt-5.6-sol", "max")])
            with patch("codex_goal_watchdog.bindings.state_dir", return_value=root), \
                 patch("codex_goal_watchdog.sessions.find_thread_rollout_path",
                       return_value=path), \
                 patch("codex_goal_watchdog.__main__.tmux_session_exists",
                       return_value=False):
                save_session_binding(
                    session="project-a", thread_id=THREAD, cwd=Path.cwd(),
                    launch_options={"primary_model": "gpt-6-astra",
                                    "primary_reasoning_effort": "xhigh"},
                    launch_profile_offset=path.stat().st_size,
                )
                output = StringIO()
                with redirect_stdout(output):
                    result = main(["start", "--session", "project-a",
                                   "--no-attach", "--dry-run"])
        self.assertEqual(0, result)
        self.assertIn("-m gpt-6-astra", output.getvalue())
        self.assertIn('model_reasoning_effort="xhigh"', output.getvalue())

    def test_unsafe_explicitly_overrides_saved_safe_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("codex_goal_watchdog.bindings.state_dir", return_value=root), \
                 patch("codex_goal_watchdog.__main__.tmux_session_exists",
                       return_value=False):
                save_session_binding(
                    session="project-a", thread_id=THREAD, cwd=Path.cwd(),
                    launch_options={"safe": True},
                )
                output = StringIO()
                with redirect_stdout(output):
                    result = main(["start", "--session", "project-a",
                                   "--unsafe", "--no-attach", "--dry-run"])
        self.assertEqual(0, result)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", output.getvalue())

    def test_new_and_explicit_other_thread_do_not_inherit_old_options(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("codex_goal_watchdog.bindings.state_dir", return_value=root), \
                 patch("codex_goal_watchdog.__main__.tmux_session_exists",
                       return_value=False):
                save_session_binding(
                    session="project-a", thread_id=THREAD, cwd=Path.cwd(),
                    launch_options={"primary_model": "gpt-6-astra",
                                    "primary_reasoning_effort": "xhigh",
                                    "safe": True},
                )
                for mode in (["--new"], ["--thread-id", OTHER_THREAD]):
                    with self.subTest(mode=mode):
                        output = StringIO()
                        with redirect_stdout(output):
                            result = main(["start", "--session", "project-a",
                                           *mode, "--no-attach", "--dry-run"])
                        self.assertEqual(0, result)
                        self.assertIn("-m gpt-5.6-sol", output.getvalue())
                        self.assertIn('model_reasoning_effort="max"', output.getvalue())
                        self.assertIn("--dangerously-bypass-approvals-and-sandbox",
                                      output.getvalue())

    def test_explicit_resume_keeps_global_latest_semantics(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("codex_goal_watchdog.bindings.state_dir", return_value=root), \
                 patch("codex_goal_watchdog.__main__.tmux_session_exists",
                       return_value=False), \
                 patch("codex_goal_watchdog.__main__.find_latest_thread_id",
                       return_value=OTHER_THREAD) as global_latest:
                save_session_binding(
                    session="project-a", thread_id=THREAD, cwd=Path.cwd()
                )
                output = StringIO()
                with redirect_stdout(output):
                    result = main(["start", "--session", "project-a", "--resume",
                                   "--no-attach", "--dry-run"])
        self.assertEqual(0, result)
        self.assertIn(f"resume {OTHER_THREAD}", output.getvalue())
        global_latest.assert_called_once()

    def test_legacy_bindingless_thread_scans_existing_rollout_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = write_rollout(root, THREAD, [turn("gpt-6-astra", "xhigh")])
            with patch("codex_goal_watchdog.bindings.state_dir", return_value=root), \
                 patch("codex_goal_watchdog.sessions.find_thread_rollout_path",
                       return_value=path), \
                 patch("codex_goal_watchdog.__main__.tmux_session_exists",
                       return_value=False), \
                 patch("codex_goal_watchdog.__main__.find_latest_thread_id",
                       return_value=THREAD):
                output = StringIO()
                with redirect_stdout(output):
                    result = main(["start", "--session", "project-a",
                                   "--resume", "--no-attach", "--dry-run"])
        self.assertEqual(0, result)
        self.assertIn("-m gpt-6-astra", output.getvalue())
        self.assertIn('model_reasoning_effort="xhigh"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
