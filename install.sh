#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION="codex-goal"
INSTALL_SERVICE=1
VENV_DIR="${CODEX_WATCH_VENV:-${HOME}/.local/share/codex-goal-watchdog/venv}"
BIN_DIR="${HOME}/.local/bin"
USER_UNIT_DIR="${HOME}/.config/systemd/user"

usage() {
    printf '%s\n' \
        "Usage: ./install.sh [--session NAME] [--no-service]" \
        "" \
        "Installs codex-watch into a private virtual environment and optionally" \
        "enables the user-level guardian service for the selected tmux session."
}

while (($#)); do
    case "$1" in
        --session)
            [[ $# -ge 2 ]] || { printf 'missing value for --session\n' >&2; exit 2; }
            SESSION="$2"
            shift 2
            ;;
        --no-service)
            INSTALL_SERVICE=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

for command in python3 tmux codex; do
    command -v "$command" >/dev/null 2>&1 || {
        printf 'required command not found: %s\n' "$command" >&2
        exit 1
    }
done

python3 - <<'PY'
import sys

if sys.version_info < (3, 11):
    raise SystemExit("codex-goal-watchdog requires Python 3.11 or newer")
PY

python3 -m venv "$VENV_DIR"
PIP_DISABLE_PIP_VERSION_CHECK=1 \
    "$VENV_DIR/bin/python" -m pip install --upgrade "$ROOT_DIR"

mkdir -p "$BIN_DIR"
ln -sfn "$VENV_DIR/bin/codex-watch" "$BIN_DIR/codex-watch"
ln -sfn "$VENV_DIR/bin/codex-watch-guardian" "$BIN_DIR/codex-watch-guardian"

if ((INSTALL_SERVICE)); then
    mkdir -p "$USER_UNIT_DIR"
    install -m 0644 \
        "$ROOT_DIR/systemd/codex-watch-guardian@.service" \
        "$USER_UNIT_DIR/codex-watch-guardian@.service"
    if systemctl --user show-environment >/dev/null 2>&1; then
        systemctl --user daemon-reload
        systemctl --user enable --now "codex-watch-guardian@${SESSION}.service"
    else
        printf '%s\n' \
            "User systemd is unavailable in this shell." \
            "The unit was installed but not enabled."
    fi
fi

printf '\nInstalled codex-goal-watchdog.\n'
printf 'Ensure %s is on PATH.\n' "$BIN_DIR"
printf 'Start from a project directory with: codex-watch --safe\n'
printf '%s\n' \
    "WARNING: omitting --safe enables Codex's dangerous approval/sandbox bypass."
if ((INSTALL_SERVICE)); then
    printf 'Guardian unit: codex-watch-guardian@%s.service\n' "$SESSION"
fi
