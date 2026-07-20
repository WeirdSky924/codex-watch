"""Portable state and log paths for installed and source-checkout usage."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


APP_NAME = "codex-goal-watchdog"


def state_dir(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    values = os.environ if env is None else env
    explicit = values.get("CODEX_WATCH_STATE_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg_state_home = values.get("XDG_STATE_HOME", "").strip()
    if xdg_state_home:
        return Path(xdg_state_home).expanduser() / APP_NAME
    user_home = Path.home() if home is None else home
    return user_home / ".local" / "state" / APP_NAME


def default_log_path(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    root = state_dir(env=env, home=home)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    return root / "watchdog.log"
