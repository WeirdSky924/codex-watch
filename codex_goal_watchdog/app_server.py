"""Small JSON-lines client for Codex app-server Goal operations."""

from __future__ import annotations

import importlib.metadata
import json
import os
import select
import subprocess
import time
from collections.abc import Callable, Mapping
from typing import Any

from .bindings import load_thread_handoff

GOAL_STATUSES = frozenset(
    {"active", "paused", "blocked", "usageLimited", "budgetLimited", "complete"}
)
PLACEHOLDER_GOAL_OBJECTIVE = "未能从旧 thread 的 rollout 提取 Goal Objective"


def native_goal_snapshot(
    goal: object,
    *,
    source_thread_id: str,
) -> dict[str, object] | None:
    if not isinstance(goal, dict):
        return None
    objective = goal.get("objective")
    status = goal.get("status")
    token_budget = goal.get("tokenBudget")
    goal_thread_id = goal.get("threadId")
    if (
        not isinstance(objective, str)
        or not objective.strip()
        or len(objective) > 20_000
        or PLACEHOLDER_GOAL_OBJECTIVE in objective
        or not source_thread_id
        or (goal_thread_id is not None and goal_thread_id != source_thread_id)
        or status not in GOAL_STATUSES
        or (
            token_budget is not None
            and (type(token_budget) is not int or token_budget < 0)
        )
    ):
        return None
    return {
        "source_thread_id": source_thread_id,
        "objective": objective,
        "status": status,
        "token_budget": token_budget,
    }


def cached_native_goal_snapshot(session: str) -> dict[str, object] | None:
    loaded = load_thread_handoff(session)
    if loaded is None:
        return None
    _path, payload = loaded
    telemetry = payload.get("telemetry")
    if not isinstance(telemetry, dict):
        return None
    source = telemetry.get("native_goal")
    if not isinstance(source, dict):
        return None
    source_thread_id = source.get("source_thread_id")
    return native_goal_snapshot(
        source,
        source_thread_id=(source_thread_id if isinstance(source_thread_id, str) else ""),
    )


class CodexAppServerError(RuntimeError):
    """An app-server process or Goal request failed."""


class CodexAppServerClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 15,
        popen_factory: Callable = subprocess.Popen,
        now: Callable[[], float] = time.monotonic,
        select_fn: Callable = select.select,
    ) -> None:
        self._timeout_seconds = max(0.1, timeout_seconds)
        self._now = now
        self._select = select_fn
        self._buffer = bytearray()
        self._pending: dict[int | str, dict[str, Any]] = {}
        self._next_id = 1
        self._process = popen_factory(
            ["codex", "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        if self._process.stdin is None or self._process.stdout is None:
            self.close()
            raise CodexAppServerError("Codex app-server pipes were not created")
        try:
            self._rpc(
                "initialize",
                {
                    "clientInfo": {
                        "name": "codex-goal-watchdog",
                        "version": self._client_version(),
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            self._send({"method": "initialized"})
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _client_version() -> str:
        try:
            return importlib.metadata.version("codex-goal-watchdog")
        except importlib.metadata.PackageNotFoundError:
            return "0.0.0"

    def __enter__(self) -> CodexAppServerClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        process = getattr(self, "_process", None)
        if process is None or process.poll() is not None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def _send(self, message: Mapping[str, Any]) -> None:
        if self._process.stdin is None:
            raise CodexAppServerError("Codex app-server stdin is closed")
        encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        try:
            self._process.stdin.write(encoded.encode("utf-8") + b"\n")
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CodexAppServerError("Codex app-server closed its input") from exc

    def _read_message(self, deadline: float) -> dict[str, Any]:
        if self._process.stdout is None:
            raise CodexAppServerError("Codex app-server stdout is closed")
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._buffer[:newline]).rstrip(b"\r")
                del self._buffer[: newline + 1]
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise CodexAppServerError(
                        "Codex app-server returned invalid JSON"
                    ) from exc
                if not isinstance(message, dict):
                    raise CodexAppServerError(
                        "Codex app-server returned a non-object message"
                    )
                return message

            remaining = deadline - self._now()
            if remaining <= 0:
                raise TimeoutError("Codex app-server request timed out")
            ready, _, _ = self._select(
                [self._process.stdout], [], [], remaining
            )
            if not ready:
                raise TimeoutError("Codex app-server request timed out")
            chunk = os.read(self._process.stdout.fileno(), 65536)
            if not chunk:
                raise CodexAppServerError(
                    "Codex app-server exited before responding"
                )
            self._buffer.extend(chunk)

    def _rpc(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"id": request_id, "method": method, "params": dict(params)})
        deadline = self._now() + self._timeout_seconds
        while True:
            message = self._pending.pop(request_id, None)
            if message is None:
                message = self._read_message(deadline)
            response_id = message.get("id")
            if response_id != request_id:
                if response_id is not None:
                    self._pending[response_id] = message
                continue
            error = message.get("error")
            if error is not None:
                detail = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                raise CodexAppServerError(f"Codex {method} failed: {detail}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise CodexAppServerError(
                    f"Codex {method} returned no result object"
                )
            return result

    def get_goal(self, thread_id: str) -> dict[str, Any] | None:
        result = self._rpc("thread/goal/get", {"threadId": thread_id})
        goal = result.get("goal")
        if goal is None:
            return None
        if not isinstance(goal, dict) or goal.get("threadId") != thread_id:
            raise CodexAppServerError(
                "Codex returned a Goal bound to a different thread"
            )
        return goal

    def set_goal(
        self,
        *,
        thread_id: str,
        objective: str,
        status: str,
        token_budget: int | None,
        origin: str = "automatic",
    ) -> dict[str, Any]:
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError("native Goal objective must be non-empty")
        if status not in GOAL_STATUSES:
            raise ValueError(f"unsupported native Goal status: {status}")
        if token_budget is not None and (
            type(token_budget) is not int or token_budget < 0
        ):
            raise ValueError("native Goal token budget must be null or non-negative")
        if origin not in {"user", "automatic"}:
            raise ValueError(f"unsupported native Goal origin: {origin}")
        result = self._rpc(
            "thread/goal/set",
            {
                "threadId": thread_id,
                "objective": objective,
                "status": status,
                "tokenBudget": token_budget,
                "origin": origin,
            },
        )
        goal = result.get("goal")
        if not isinstance(goal, dict) or goal.get("threadId") != thread_id:
            raise CodexAppServerError("Codex did not return the restored native Goal")
        return goal
