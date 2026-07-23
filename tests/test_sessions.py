import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from codex_goal_watchdog.sessions import (
    compaction_event_exists_after,
    find_active_cli_thread_id,
    find_latest_thread_id,
    find_new_thread_id,
    find_thread_rollout_path,
    wait_for_new_thread_id,
)


class SessionResolverTests(unittest.TestCase):
    def _write_session(
        self,
        root: Path,
        *,
        thread_id: str,
        cwd: str,
        started_at: datetime,
        source="cli",
    ) -> Path:
        path = root / f"rollout-{thread_id}.jsonl"
        payload = {
            "timestamp": started_at.isoformat().replace("+00:00", "Z"),
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "cwd": cwd,
                "source": source,
                "timestamp": started_at.isoformat().replace("+00:00", "Z"),
            },
        }
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return path

    def test_find_latest_thread_id_filters_by_cwd(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_session(
                root,
                thread_id="550e8400-e29b-41d4-a716-446655440000",
                cwd="/workspace/target",
                started_at=datetime(2026, 7, 14, 10, tzinfo=timezone.utc),
            )
            self._write_session(
                root,
                thread_id="550e8400-e29b-41d4-a716-446655440001",
                cwd="/workspace/other",
                started_at=datetime(2026, 7, 14, 11, tzinfo=timezone.utc),
            )

            actual = find_latest_thread_id(
                cwd=Path("/workspace/target"), sessions_root=root
            )

        self.assertEqual("550e8400-e29b-41d4-a716-446655440000", actual)

    def test_find_new_thread_id_uses_session_start_not_file_mtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_path = self._write_session(
                root,
                thread_id="550e8400-e29b-41d4-a716-446655440000",
                cwd="/workspace/target",
                started_at=datetime(2026, 7, 14, 10, tzinfo=timezone.utc),
            )
            old_path.touch()
            self._write_session(
                root,
                thread_id="550e8400-e29b-41d4-a716-446655440002",
                cwd="/workspace/target",
                started_at=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
            )

            actual = find_new_thread_id(
                cwd=Path("/workspace/target"),
                started_after=datetime(2026, 7, 14, 11, tzinfo=timezone.utc),
                sessions_root=root,
            )

        self.assertEqual("550e8400-e29b-41d4-a716-446655440002", actual)

    def test_find_new_thread_id_uses_shell_snapshot_before_rollout_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sessions_root = root / "sessions"
            snapshots_root = root / "shell_snapshots"
            snapshots_root.mkdir()
            thread_id = "550e8400-e29b-41d4-a716-446655440003"
            (snapshots_root / f"{thread_id}.1784693226807668417.sh").write_text(
                "# Snapshot file\n",
                encoding="utf-8",
            )

            actual = find_new_thread_id(
                cwd=Path("/workspace/target"),
                started_after=datetime(2026, 7, 22, 1, tzinfo=timezone.utc),
                sessions_root=sessions_root,
                shell_snapshots_root=snapshots_root,
            )

        self.assertEqual(thread_id, actual)

    def test_find_active_cli_thread_uses_open_rollout_and_ignores_subagents(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sessions_root = root / "sessions"
            sessions_root.mkdir()
            proc_root = root / "proc"
            pane_pid = 100
            child_pid = 101
            unrelated_pid = 999
            for pid in (pane_pid, child_pid, unrelated_pid):
                (proc_root / str(pid) / "task" / str(pid)).mkdir(parents=True)
                (proc_root / str(pid) / "fd").mkdir()
            (proc_root / str(pane_pid) / "task" / str(pane_pid) / "children").write_text(
                str(child_pid), encoding="utf-8"
            )
            (proc_root / str(child_pid) / "task" / str(child_pid) / "children").write_text(
                "", encoding="utf-8"
            )
            (proc_root / str(unrelated_pid) / "task" / str(unrelated_pid) / "children").write_text(
                "", encoding="utf-8"
            )
            old_main = self._write_session(
                sessions_root,
                thread_id="550e8400-e29b-41d4-a716-446655440000",
                cwd="/workspace/target",
                started_at=datetime(2026, 7, 14, 10, tzinfo=timezone.utc),
            )
            new_main = self._write_session(
                sessions_root,
                thread_id="550e8400-e29b-41d4-a716-446655440001",
                cwd="/workspace/target",
                started_at=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
            )
            subagent = self._write_session(
                sessions_root,
                thread_id="550e8400-e29b-41d4-a716-446655440002",
                cwd="/workspace/target",
                started_at=datetime(2026, 7, 14, 13, tzinfo=timezone.utc),
                source={"subagent": {"thread_spawn": {"depth": 1}}},
            )
            unrelated = self._write_session(
                sessions_root,
                thread_id="550e8400-e29b-41d4-a716-446655440003",
                cwd="/workspace/target",
                started_at=datetime(2026, 7, 14, 14, tzinfo=timezone.utc),
            )
            for fd, path in enumerate((old_main, new_main, subagent), start=3):
                (proc_root / str(child_pid) / "fd" / str(fd)).symlink_to(path)
            (proc_root / str(unrelated_pid) / "fd" / "3").symlink_to(unrelated)

            actual = find_active_cli_thread_id(
                pane_pid=pane_pid,
                cwd=Path("/workspace/target"),
                proc_root=proc_root,
            )

        self.assertEqual("550e8400-e29b-41d4-a716-446655440001", actual)

    def test_find_thread_rollout_path_and_compaction_event(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            thread_id = "550e8400-e29b-41d4-a716-446655440000"
            path = self._write_session(
                root,
                thread_id=thread_id,
                cwd="/workspace/target",
                started_at=datetime(2026, 7, 14, 10, tzinfo=timezone.utc),
            )
            offset = path.stat().st_size
            with path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {"type": "context_compacted"},
                        }
                    )
                    + "\n"
                )

            actual_path = find_thread_rollout_path(
                thread_id=thread_id, sessions_root=root
            )

            self.assertEqual(path, actual_path)
            self.assertTrue(compaction_event_exists_after(path, offset=offset))

    def test_wait_for_new_thread_allows_startup_update_to_reset_deadline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            started_at = datetime(2026, 7, 22, 1, tzinfo=timezone.utc)
            callback_calls = []

            def on_wait():
                callback_calls.append(True)
                self._write_session(
                    root,
                    thread_id="550e8400-e29b-41d4-a716-446655440000",
                    cwd="/workspace/target",
                    started_at=started_at,
                )
                return True

            thread_id = wait_for_new_thread_id(
                cwd=Path("/workspace/target"),
                started_after=started_at,
                sessions_root=root,
                timeout_seconds=1,
                on_wait=on_wait,
                sleeper=lambda _seconds: None,
                now=iter([0.0, 0.5, 2.0, 2.1]).__next__,
            )

        self.assertEqual("550e8400-e29b-41d4-a716-446655440000", thread_id)
        self.assertEqual([True], callback_calls)


if __name__ == "__main__":
    unittest.main()
