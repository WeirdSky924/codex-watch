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
        "installs the user-level guardian service for the selected tmux session." \
        "An existing guardian keeps its current enabled/running state."
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

SERVICE_UNIT="codex-watch-guardian@${SESSION}.service"
SERVICE_UNIT_FILE="$USER_UNIT_DIR/codex-watch-guardian@.service"
SYSTEMD_USER_AVAILABLE=0
SERVICE_WAS_PRESENT=0
SERVICE_WAS_ENABLED=0
SERVICE_WAS_ACTIVE=0
if ((INSTALL_SERVICE)) && command -v systemctl >/dev/null 2>&1 \
    && systemctl --user show-environment >/dev/null 2>&1; then
    SYSTEMD_USER_AVAILABLE=1
    [[ -f "$SERVICE_UNIT_FILE" ]] && SERVICE_WAS_PRESENT=1
    systemctl --user is-enabled "$SERVICE_UNIT" >/dev/null 2>&1 \
        && SERVICE_WAS_ENABLED=1 || true
    systemctl --user is-active "$SERVICE_UNIT" >/dev/null 2>&1 \
        && SERVICE_WAS_ACTIVE=1 || true
fi

python3 -m venv "$VENV_DIR"
PIP_DISABLE_PIP_VERSION_CHECK=1 \
    "$VENV_DIR/bin/python" -m pip install --upgrade "$ROOT_DIR"

INSTALLED_VERSION="$("$VENV_DIR/bin/python" -c \
    'import codex_goal_watchdog; print(codex_goal_watchdog.__version__)')"
if [[ -z "$INSTALLED_VERSION" ]]; then
    printf '%s\n' "watchdog installation did not expose a package version" >&2
    exit 1
fi

mkdir -p "$BIN_DIR"
ln -sfn "$VENV_DIR/bin/codex-watch" "$BIN_DIR/codex-watch"
ln -sfn "$VENV_DIR/bin/codex-watch-guardian" "$BIN_DIR/codex-watch-guardian"

if ((INSTALL_SERVICE)); then
    mkdir -p "$USER_UNIT_DIR"
    install -m 0644 \
        "$ROOT_DIR/systemd/codex-watch-guardian@.service" \
        "$USER_UNIT_DIR/codex-watch-guardian@.service"
    if ((SYSTEMD_USER_AVAILABLE)); then
        systemctl --user daemon-reload
        if ((SERVICE_WAS_ENABLED)); then
            systemctl --user enable --now "$SERVICE_UNIT"
        elif ((SERVICE_WAS_ACTIVE)); then
            systemctl --user start "$SERVICE_UNIT"
        elif ((SERVICE_WAS_PRESENT)); then
            systemctl --user disable --now "$SERVICE_UNIT" >/dev/null 2>&1 || true
            printf '%s\n' \
                "Guardian state preserved: stopped and disabled." \
                "Run codex-watch explicitly when this session should resume."
        else
            systemctl --user enable --now "$SERVICE_UNIT"
        fi
    else
        printf '%s\n' \
            "User systemd is unavailable in this shell." \
            "The unit was installed but not enabled."
    fi
fi

printf '\nInstalled codex-goal-watchdog.\n'
printf 'Installed version: %s\n' "$INSTALLED_VERSION"
printf 'Ensure %s is on PATH.\n' "$BIN_DIR"
printf 'Start from a project directory with: codex-watch --safe\n'
printf '%s\n' \
    "WARNING: omitting --safe enables Codex's dangerous approval/sandbox bypass."
if ((INSTALL_SERVICE)); then
    printf 'Guardian unit: codex-watch-guardian@%s.service\n' "$SESSION"
fi
