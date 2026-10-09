import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_goal_watchdog.bindings import (
    load_thread_handoff,
    save_session_binding,
    save_thread_handoff,
)
from codex_goal_watchdog.app_server import CodexAppServerClient
from codex_goal_watchdog.monitor import run_monitor
from codex_goal_watchdog.recovery import (
    CompactionTimeoutError,
    CompactionUpstreamError,
    RecoveryConfig,
)
from codex_goal_watchdog.sessions import TaskFailure
from codex_goal_watchdog.rotation_state import find_rotation_goal_objective
from codex_goal_watchdog.tmux_control import RecoveryStep, _execute_steps_unlocked
from codex_goal_watchdog.rotation_state import (
    _mark_rotation_handoff_rebound,
    complete_native_goal_rotation,
    restore_native_goal_for_thread,
    pending_thread_rotation_prompt,
    pending_thread_rotation_is_valid,
    pending_thread_rotation_marker,
    set_pending_thread_rotation,
    write_thread_rotation_handoff,
)


THREAD_ID = "550e8400-e29b-41d4-a716-446655440000"
FAKE_APP_SERVER = r"""
import json
import sys

goal = None
for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    request_id = request.get("id")
    if method == "initialize":
        print(json.dumps({"method": "remoteControl/status/changed", "params": {}}), flush=True)
        response = {"userAgent": "fake"}
    elif method == "thread/goal/get":
        response = {"goal": goal}
    elif method == "thread/goal/set":
        params = request["params"]
        goal = {
            "threadId": params["threadId"],
            "objective": params["objective"],
            "status": params["status"],
            "tokenBudget": params["tokenBudget"],
        }
        response = {"goal": goal}
    else:
        continue
    if request_id is not None:
        print(json.dumps({"id": request_id, "result": response}), flush=True)
"""


def _stall_lines() -> list[str]:
    return [
        "Pursuing goal (4m)\n",
        "■ stream disconnected before completion: codex upstream stalled: "
        "no real data for 5m0s, connection recycled\n",
    ]


class MonitorRecoveryBoundaryTests(unittest.TestCase):
    def test_handoff_keeps_last_valid_native_goal_when_source_has_placeholder(self):
        source_thread_id = "550e8400-e29b-41d4-a716-446655440000"
        objective = "Goal ID: FE-CREATOR-8 continue current ACTIVE plan"

        class FakeAppServer:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return None

            def get_goal(self, _thread_id):
                return {
                    "threadId": source_thread_id,
                    "objective": (
                        "未能从旧 thread 的 rollout 提取 Goal Objective。"
                    ),
                    "status": "blocked",
                    "tokenBudget": None,
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            state_root = Path(temp_dir)
            with patch("codex_goal_watchdog.bindings.state_dir", return_value=state_root):
                save_thread_handoff(
                    session="codex-goal",
                    thread_id=source_thread_id,
                    cwd=Path("/workspace/project"),
                    reason="upstream_access_denied",
                    goal_objective=objective,
                    telemetry={
                        "native_goal": {
                            "source_thread_id": source_thread_id,
                            "objective": objective,
                            "status": "blocked",
                            "token_budget": None,
                        },
                        "rebound_thread_id": "550e8400-e29b-41d4-a716-446655440001",
                    },
                    state_root=state_root,
                )
                with patch(
                    "codex_goal_watchdog.rotation_state.CodexAppServerClient",
                    return_value=FakeAppServer(),
                ):
                    path = write_thread_rotation_handoff(
                        session="codex-goal",
                        thread_id="550e8400-e29b-41d4-a716-446655440001",
                        cwd=Path("/workspace/project"),
                        reason="upstream_access_denied",
                    )
                loaded = load_thread_handoff("codex-goal", state_root=state_root)

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(path, loaded[0])
        self.assertEqual(objective, loaded[1]["goal_objective"])
        self.assertEqual(
            objective,
            loaded[1]["telemetry"]["native_goal"]["objective"],
        )
        self.assertEqual(
            "550e8400-e29b-41d4-a716-446655440001",
            loaded[1]["telemetry"]["native_goal"]["source_thread_id"],
        )

    def test_handoff_does_not_reuse_cached_goal_after_clear(self):
        source_thread_id = "550e8400-e29b-41d4-a716-446655440000"
        cleared_thread_id = "550e8400-e29b-41d4-a716-446655440002"
        objective = "Goal ID: FE-CREATOR-8 continue current ACTIVE plan"

        class FakeAppServer:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return None

            def get_goal(self, _thread_id):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            state_root = Path(temp_dir)
            with patch("codex_goal_watchdog.bindings.state_dir", return_value=state_root):
                save_thread_handoff(
                    session="codex-goal",
                    thread_id=source_thread_id,
                    cwd=Path("/workspace/project"),
                    reason="upstream_access_denied",
                    goal_objective=objective,
                    telemetry={
                        "native_goal": {
                            "source_thread_id": source_thread_id,
                            "objective": objective,
                            "status": "active",
                            "token_budget": None,
                        }
                    },
                    state_root=state_root,
                )
                with patch(
                    "codex_goal_watchdog.rotation_state.CodexAppServerClient",
                    return_value=FakeAppServer(),
                ):
                    write_thread_rotation_handoff(
                        session="codex-goal",
                        thread_id=cleared_thread_id,
                        cwd=Path("/workspace/project"),
                        reason="upstream_access_denied",
                    )
                loaded = load_thread_handoff("codex-goal", state_root=state_root)

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertNotIn("native_goal", loaded[1]["telemetry"])
        self.assertEqual("", loaded[1]["goal_objective"])

    def test_restore_native_goal_reads_back_objective_and_blocked_status(self):
        source_thread_id = "550e8400-e29b-41d4-a716-446655440000"
        target_thread_id = "550e8400-e29b-41d4-a716-446655440001"
        objective = "Goal ID: FE-CREATOR-8 continue current ACTIVE plan"

        class FakeAppServer:
            def __init__(self):
                self.goal = None
                self.set_calls = []

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return None

            def get_goal(self, thread_id):
                return self.goal if self.goal and self.goal["threadId"] == thread_id else None

            def set_goal(self, **kwargs):
                self.set_calls.append(kwargs)
                self.goal = {
                    "threadId": kwargs["thread_id"],
                    "objective": kwargs["objective"],
                    "status": kwargs["status"],
                    "tokenBudget": kwargs["token_budget"],
                }
                return self.goal

        app_server = FakeAppServer()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_root = Path(temp_dir)
            with patch("codex_goal_watchdog.bindings.state_dir", return_value=state_root):
                save_session_binding(
                    session="codex-goal",
                    thread_id=target_thread_id,
                    cwd=Path("/workspace/project"),
                    verification_pending=True,
                    last_recovery_reason="upstream_access_denied",
                    state_root=state_root,
                )
                save_thread_handoff(
                    session="codex-goal",
                    thread_id=source_thread_id,
                    cwd=Path("/workspace/project"),
                    reason="upstream_access_denied",
                    goal_objective=objective,
                    telemetry={
                        "native_goal": {
                            "source_thread_id": source_thread_id,
                            "objective": objective,
                            "status": "blocked",
                            "token_budget": None,
                        },
                        "rebound_thread_id": target_thread_id,
                    },
                    state_root=state_root,
                )
                restored = restore_native_goal_for_thread(
                    "codex-goal",
                    target_thread_id,
                    app_server_factory=lambda: app_server,
                )

        self.assertEqual(1, len(app_server.set_calls))
        self.assertEqual("automatic", app_server.set_calls[0]["origin"])
        self.assertEqual(objective, restored["objective"])
        self.assertEqual("blocked", restored["status"])

    def test_goal_objective_falls_back_to_terminal_capture_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            thread_id = THREAD_ID
            path = root / f"rollout-{thread_id}.jsonl"
            events = [
                {"type": "session_meta", "payload": {"id": thread_id, "cwd": "/workspace/project", "source": "cli", "timestamp": "2026-09-09T05:00:00Z"}},
                {"type": "response_item", "payload": {"type": "custom_tool_call_output", "output": [{"type": "input_text", "text": "• Goal active Objective: Goal ID: FE-CREATOR-8\n继续最新计划\nTime: 2m."}]}},
            ]
            path.write_text("\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n", encoding="utf-8")

            with patch(
                "codex_goal_watchdog.rotation_state.find_thread_rollout_path",
                return_value=path,
            ):
                objective = find_rotation_goal_objective(thread_id)

        self.assertEqual("Goal ID: FE-CREATOR-8 继续最新计划", objective)

    @patch("codex_goal_watchdog.monitor.complete_native_goal_rotation")
    def test_compaction_timeout_falls_back_to_fresh_thread_rotation(self, _complete_rotation):
        calls = []
        handoffs = []

        def execute(target, steps):
            calls.append((target, steps))
            if any(step.kind == "wait_compaction" for step in steps):
                raise CompactionTimeoutError("compaction timed out")

        run_monitor(
            lines=_stall_lines(),
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID),
            resolve_goal_objective=lambda _thread_id: "Goal ID: FE-CREATOR-8",
            write_thread_handoff=lambda **kwargs: (
                handoffs.append(kwargs) or Path("/state/handoffs/latest.json")
            ),
            mark_thread_rotation=lambda _count, _reason, _thread_id: None,
            now=iter([100.0, 101.0]).__next__,
            execute=execute,
            log=lambda _message: None,
        )

        self.assertEqual(2, len(calls))
        self.assertEqual("compaction_timeout", handoffs[0]["reason"])
        self.assertFalse(any(step.kind == "wait_compaction" for step in calls[1][1]))

    def test_compaction_upstream_error_reuses_pinned_thread_without_rotation(self):
        calls = []
        handoffs = []

        def execute(target, steps):
            calls.append((target, steps))
            if any(step.kind == "wait_compaction" for step in steps):
                raise CompactionUpstreamError("retryable_http_503")

        run_monitor(
            lines=_stall_lines(),
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID),
            resolve_recovery_incident=lambda _thread_id: (
                "turn-stall",
                "codex_upstream_stalled",
            ),
            now=iter([100.0, 101.0]).__next__,
            execute=execute,
            log=lambda _message: None,
        )

        self.assertEqual(2, len(calls))
        self.assertEqual([], handoffs)
        self.assertTrue(
            any(
                step.kind == "shell_command"
                and f"resume {THREAD_ID}" in step.value
                for step in calls[1][1]
            )
        )

    @patch("codex_goal_watchdog.tmux_control.find_latest_task_failure_after")
    @patch(
        "codex_goal_watchdog.tmux_control.find_thread_rollout_path"
    )
    @patch(
        "codex_goal_watchdog.tmux_control.wait_for_pane_state",
        side_effect=TimeoutError("compact model did not start"),
    )
    def test_compact_startup_failure_is_classified_as_upstream_failure(
        self,
        _wait_mock,
        rollout_path_mock,
        failure_mock,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "rollout.jsonl"
            path.write_text("", encoding="utf-8")
            rollout_path_mock.return_value = path
            failure_mock.return_value = TaskFailure(
                incident_id="turn-503-start",
                message="unexpected status 503 Service Unavailable",
                codex_error_info=None,
            )

            with self.assertRaises(CompactionUpstreamError) as raised:
                _execute_steps_unlocked(
                    "codex-goal",
                    [
                        RecoveryStep("mark_compaction", THREAD_ID),
                        RecoveryStep("wait_codex", "30"),
                    ],
                    sleeper=lambda _seconds: None,
                )

        self.assertEqual("retryable_http_503", raised.exception.reason)

    @patch("codex_goal_watchdog.monitor.complete_native_goal_rotation")
    def test_compaction_access_denied_keeps_new_thread_rotation(self, _complete_rotation):
        calls = []
        handoffs = []

        def execute(target, steps):
            calls.append((target, steps))
            if any(step.kind == "wait_compaction" for step in steps):
                raise CompactionUpstreamError("upstream_access_denied")

        run_monitor(
            lines=_stall_lines(),
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID),
            resolve_recovery_incident=lambda _thread_id: (
                "turn-stall",
                "codex_upstream_stalled",
            ),
            resolve_goal_objective=lambda _thread_id: "Goal ID: FE-CREATOR-8",
            write_thread_handoff=lambda **kwargs: (
                handoffs.append(kwargs) or Path("/state/latest.json")
            ),
            now=iter([100.0, 101.0]).__next__,
            execute=execute,
            log=lambda _message: None,
        )

        self.assertEqual(2, len(calls))
        fresh_commands = [
            step.value for step in calls[1][1] if step.kind == "shell_command"
        ]
        self.assertEqual(1, len(fresh_commands))
        self.assertNotIn(f"resume {THREAD_ID}", fresh_commands[0])
        self.assertEqual("upstream_access_denied", handoffs[0]["reason"])

    def test_generic_timeout_during_compaction_recovery_is_not_rotation(self):
        calls = []

        def execute(target, steps):
            calls.append((target, steps))
            if any(step.kind == "wait_compaction" for step in steps):
                raise TimeoutError("unrelated recovery timeout")

        with self.assertRaisesRegex(TimeoutError, "unrelated recovery timeout"):
            run_monitor(
                lines=_stall_lines(),
                target="codex-goal",
                config=RecoveryConfig(thread_id=THREAD_ID),
                now=iter([100.0, 101.0]).__next__,
                execute=execute,
                log=lambda _message: None,
            )

        self.assertEqual(1, len(calls))

    def test_repeated_temporary_upstream_errors_never_mark_thread_rotation(self):
        calls = []
        marked_rotations = []
        incidents = iter(
            [
                ("turn-503-a", "retryable_http_503"),
                ("turn-503-b", "retryable_http_503"),
            ]
        )

        run_monitor(
            lines=[
                "Pursuing goal (4m)\n",
                "■ unexpected status 503 Service Unavailable: upstream failed\n",
                "Pursuing goal (5m)\n",
                "■ unexpected status 503 Service Unavailable: upstream failed\n",
            ],
            target="codex-goal",
            config=RecoveryConfig(thread_id=THREAD_ID, cooldown_seconds=0),
            resolve_recovery_incident=lambda _thread_id: next(incidents),
            mark_thread_rotation=lambda count, _reason, _thread_id: (
                marked_rotations.append(count)
            ),
            write_thread_handoff=lambda **_kwargs: Path("/state/should-not-exist"),
            execute=lambda target, steps: calls.append((target, steps)),
            now=iter([100.0, 101.0, 102.0, 103.0]).__next__,
            log=lambda _message: None,
        )

        self.assertEqual(2, len(calls))
        self.assertEqual([], marked_rotations)
        self.assertTrue(
            all(
                any(
                    step.kind == "shell_command"
                    and f"resume {THREAD_ID}" in step.value
                    for step in steps
                )
                for _, steps in calls
            )
        )


class RotationStateBoundaryTests(unittest.TestCase):
    def test_app_server_goal_get_and_set_round_trip(self):
        def fake_popen(_command, **kwargs):
            return subprocess.Popen(
                [sys.executable, "-u", "-c", FAKE_APP_SERVER], **kwargs
            )

        with CodexAppServerClient(popen_factory=fake_popen) as client:
            self.assertIsNone(client.get_goal("source-thread"))
            restored = client.set_goal(
                thread_id="new-thread",
                objective="Continue the current Goal",
                status="blocked",
                token_budget=None,
            )
            self.assertEqual("new-thread", restored["threadId"])
            self.assertEqual("blocked", restored["status"])
            self.assertIsNone(restored["tokenBudget"])
            self.assertEqual("Continue the current Goal", restored["objective"])
            self.assertEqual("new-thread", client.get_goal("new-thread")["threadId"])

    def test_native_rotation_submits_only_after_active_goal_restore(self):
        order = []
        objective = "Goal ID: FE-CREATOR-8 continue current ACTIVE plan"

        def restore(_target, _source_thread_id):
            order.append("restore")
            return "new-thread", {"objective": objective, "status": "active"}

        def execute(_target, steps):
            order.append("submit")
            self.assertEqual("text", steps[0].kind)
            self.assertIn("native Goal", steps[0].value)
            self.assertIn(objective, steps[0].value)
            self.assertNotIn("创建 Goal", steps[0].value)

        complete_native_goal_rotation(
            "codex-goal",
            THREAD_ID,
            "upstream_access_denied",
            "/state/handoff.json",
            execute,
            restore_thread_goal=restore,
        )

        self.assertEqual(["restore", "submit"], order)

    def test_native_rotation_does_not_submit_blocked_goal(self):
        calls = []
        complete_native_goal_rotation(
            "codex-goal",
            THREAD_ID,
            "upstream_access_denied",
            "/state/handoff.json",
            lambda _target, steps: calls.append(steps),
            restore_thread_goal=lambda _target, _source: (
                "new-thread",
                {"objective": "Goal ID: FE-CREATOR-8", "status": "blocked"},
            ),
        )
        self.assertEqual([], calls)

    @patch("codex_goal_watchdog.rotation_state.subprocess.run")
    def test_rotation_count_is_written_after_reason_and_source(
        self,
        run_mock,
    ):
        set_pending_thread_rotation(
            "codex-goal",
            2,
            reason="compaction_timeout",
            source_thread_id=THREAD_ID,
        )

        writes = [call.args[0][-2:] for call in run_mock.call_args_list]
        self.assertEqual(
            [
                ["@codex_pending_thread_rotation_reason", "compaction_timeout"],
                ["@codex_pending_thread_rotation_thread_id", THREAD_ID],
                ["@codex_pending_thread_rotation_count", "2"],
            ],
            writes,
        )

    def test_pending_thread_rotation_requires_supported_reason_and_source(self):
        values = {
            "@codex_pending_thread_rotation_count": "1",
            "@codex_pending_thread_rotation_reason": "retryable_http_503",
            "@codex_pending_thread_rotation_thread_id": THREAD_ID,
        }

        def runner(command, **_kwargs):
            class Result:
                returncode = 0
                stdout = values.get(command[-1], "")

            return Result()

        self.assertFalse(
            pending_thread_rotation_is_valid(
                "codex-goal", thread_id=THREAD_ID, runner=runner
            )
        )
        values["@codex_pending_thread_rotation_reason"] = (
            "upstream_access_denied"
        )
        values["@codex_pending_thread_rotation_thread_id"] = THREAD_ID
        self.assertTrue(
            pending_thread_rotation_is_valid(
                "codex-goal", thread_id=THREAD_ID, runner=runner
            )
        )
        values["@codex_pending_thread_rotation_reason"] = "compaction_timeout"
        self.assertTrue(
            pending_thread_rotation_is_valid(
                "codex-goal", thread_id=THREAD_ID, runner=runner
            )
        )
        values["@codex_pending_thread_rotation_thread_id"] = (
            "550e8400-e29b-41d4-a716-446655440001"
        )
        self.assertFalse(
            pending_thread_rotation_is_valid(
                "codex-goal", thread_id=THREAD_ID, runner=runner
            )
        )
        values.pop("@codex_pending_thread_rotation_thread_id")
        self.assertFalse(
            pending_thread_rotation_is_valid(
                "codex-goal", thread_id=THREAD_ID, runner=runner
            )
        )

    @patch("codex_goal_watchdog.rotation_state.clear_pending_thread_rotation")
    def test_marker_writer_clears_stale_state_for_retryable_reason(
        self, clear_mock
    ):
        persisted = set_pending_thread_rotation(
            "codex-goal",
            2,
            reason="retryable_http_503",
            source_thread_id=THREAD_ID,
        )

        self.assertFalse(persisted)
        clear_mock.assert_called_once_with("codex-goal")

    @patch("codex_goal_watchdog.tmux_control.find_latest_task_failure_after")
    @patch(
        "codex_goal_watchdog.tmux_control.compaction_event_exists_after",
        return_value=False,
    )
    @patch("codex_goal_watchdog.tmux_control.find_thread_rollout_path")
    def test_compaction_wait_exposes_new_upstream_failure(
        self, rollout_path_mock, _compaction_mock, failure_mock
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "rollout.jsonl"
            path.write_text("", encoding="utf-8")
            rollout_path_mock.return_value = path
            failure_mock.return_value = TaskFailure(
                incident_id="turn-503-new",
                message="unexpected status 503 Service Unavailable",
                codex_error_info=None,
            )
            from codex_goal_watchdog.tmux_control import CompactionUpstreamError

            with self.assertRaises(CompactionUpstreamError) as raised:
                _execute_steps_unlocked(
                    "codex-goal",
                    [
                        RecoveryStep("mark_compaction", THREAD_ID),
                        RecoveryStep(
                            "wait_compaction", THREAD_ID, timeout_seconds=600
                        ),
                    ],
                    sleeper=lambda _seconds: None,
                )

        self.assertEqual("retryable_http_503", raised.exception.reason)

    def test_find_latest_task_failure_after_reads_only_new_rollout_events(self):
        from codex_goal_watchdog.sessions import find_latest_task_failure_after

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "rollout.jsonl"
            path.write_text("session metadata\n", encoding="utf-8")
            offset = path.stat().st_size
            with path.open("a", encoding="utf-8") as stream:
                stream.write(
                    '{"type":"event_msg","payload":{"type":"task_complete",'
                    '"turn_id":"turn-503-new","error":{"message":"unexpected '
                    'status 503 Service Unavailable"}}}\n'
                )

            failure = find_latest_task_failure_after(path, offset=offset)

        self.assertIsNotNone(failure)
        assert failure is not None
        self.assertEqual("turn-503-new", failure.incident_id)

    def test_pending_rotation_prompt_uses_supported_bounded_handoff(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_root = Path(temp_dir)
            with patch("codex_goal_watchdog.bindings.state_dir", return_value=state_root):
                handoff = save_thread_handoff(
                    session="codex-goal",
                    thread_id=THREAD_ID,
                    cwd=Path("/workspace/project"),
                    reason="compaction_timeout",
                    goal_objective="Goal ID: FE-CREATOR-8",
                    telemetry={},
                    state_root=state_root,
                )
                prompt = pending_thread_rotation_prompt(
                    "codex-goal",
                    thread_id=THREAD_ID,
                    goal_state="pursuing",
                )

        self.assertIsNotNone(prompt)
        assert prompt is not None
        self.assertIn(str(handoff), prompt)
        self.assertIn("Goal ID: FE-CREATOR-8", prompt)

    def test_pending_rotation_prompt_ignores_legacy_health_handoff(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_root = Path(temp_dir)
            with patch("codex_goal_watchdog.bindings.state_dir", return_value=state_root):
                save_thread_handoff(
                    session="codex-goal",
                    thread_id=THREAD_ID,
                    cwd=Path("/workspace/project"),
                    reason="repeated_command",
                    goal_objective="Goal ID: FE-CREATOR-8",
                    telemetry={},
                    state_root=state_root,
                )
                prompt = pending_thread_rotation_prompt(
                    "codex-goal",
                    thread_id=THREAD_ID,
                    goal_state="pursuing",
                )

        self.assertIsNone(prompt)

    def test_stale_rotation_handoff_requires_the_exact_rebound_thread_id(self):
        old_thread_id = "550e8400-e29b-41d4-a716-446655440000"
        current_thread_id = "550e8400-e29b-41d4-a716-446655440001"
        objective = "Goal ID: FE-CREATOR-8 continue current ACTIVE plan"
        native_goal = {
            "source_thread_id": old_thread_id,
            "objective": objective,
            "status": "active",
            "token_budget": None,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            state_root = Path(temp_dir)
            with patch("codex_goal_watchdog.bindings.state_dir", return_value=state_root):
                save_thread_handoff(
                    session="codex-goal",
                    thread_id=old_thread_id,
                    cwd=Path("/workspace/project"),
                    reason="upstream_access_denied",
                    goal_objective=objective,
                    telemetry={"native_goal": native_goal},
                    state_root=state_root,
                )
                save_session_binding(
                    session="codex-goal",
                    thread_id=current_thread_id,
                    cwd=Path("/workspace/project"),
                    verification_pending=True,
                    last_recovery_reason="retryable_http_503",
                    state_root=state_root,
                )
                prompt = pending_thread_rotation_prompt(
                    "codex-goal",
                    thread_id=current_thread_id,
                    goal_state="pursuing",
                )
                save_thread_handoff(
                    session="codex-goal",
                    thread_id=old_thread_id,
                    cwd=Path("/workspace/project"),
                    reason="upstream_access_denied",
                    goal_objective=objective,
                    telemetry={
                        "native_goal": native_goal,
                        "rebound_thread_id": current_thread_id,
                    },
                    state_root=state_root,
                )
                rebound_prompt = pending_thread_rotation_prompt(
                    "codex-goal",
                    thread_id=current_thread_id,
                    goal_state="pursuing",
                )

        self.assertIsNone(prompt)
        self.assertIsNotNone(rebound_prompt)

    def test_rotation_handoff_records_the_bound_new_thread(self):
        old_thread_id = "550e8400-e29b-41d4-a716-446655440000"
        new_thread_id = "550e8400-e29b-41d4-a716-446655440001"

        with tempfile.TemporaryDirectory() as temp_dir:
            state_root = Path(temp_dir)
            with patch("codex_goal_watchdog.bindings.state_dir", return_value=state_root):
                save_thread_handoff(
                    session="codex-goal",
                    thread_id=old_thread_id,
                    cwd=Path("/workspace/project"),
                    reason="upstream_access_denied",
                    goal_objective="Goal ID: FE-CREATOR-8",
                    telemetry={},
                    state_root=state_root,
                )
                _mark_rotation_handoff_rebound(
                    "codex-goal",
                    old_thread_id=old_thread_id,
                    new_thread_id=new_thread_id,
                    cwd=Path("/workspace/project"),
                )
                loaded = load_thread_handoff("codex-goal", state_root=state_root)

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(
            new_thread_id,
            loaded[1]["telemetry"]["rebound_thread_id"],
        )

    @patch("codex_goal_watchdog.rotation_state.clear_pending_thread_rotation")
    @patch(
        "codex_goal_watchdog.rotation_state.pending_thread_rotation_is_valid",
        return_value=False,
    )
    @patch(
        "codex_goal_watchdog.rotation_state.pending_thread_rotation_count",
        return_value=1,
    )
    def test_guardian_clears_invalid_pending_rotation_marker(
        self, _count_mock, _valid_mock, clear_mock
    ):
        config = RecoveryConfig(thread_id=THREAD_ID)

        self.assertFalse(
            pending_thread_rotation_marker(
                "codex-goal",
                thread_id=config.thread_id,
            )
        )
        clear_mock.assert_called_once_with("codex-goal")


if __name__ == "__main__":
    unittest.main()
