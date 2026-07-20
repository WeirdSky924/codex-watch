#!/usr/bin/env bash
set -euo pipefail

SESSION="codex-goal"
PURGE_STATE=0
VENV_DIR="${CODEX_WATCH_VENV:-${HOME}/.local/share/codex-goal-watchdog/venv}"
BIN_DIR="${HOME}/.local/bin"
USER_UNIT_DIR="${HOME}/.config/systemd/user"

usage() {
    printf '%s\n' \
        "Usage: ./uninstall.sh [--session NAME] [--purge-state]" \
        "" \
        "Removes the private installation and user service. Logs are retained" \
        "unless --purge-state is specified."
}

while (($#)); do
    case "$1" in
        --session)
            [[ $# -ge 2 ]] || { printf 'missing value for --session\n' >&2; exit 2; }
            SESSION="$2"
            shift 2
            ;;
        --purge-state)
            PURGE_STATE=1
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

if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now \
        "codex-watch-guardian@${SESSION}.service" >/dev/null 2>&1 || true
fi
rm -f "$USER_UNIT_DIR/codex-watch-guardian@.service"
if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload >/dev/null 2>&1 || true
fi

for command in codex-watch codex-watch-guardian; do
    link="$BIN_DIR/$command"
    if [[ -L "$link" && "$(readlink -f "$link")" == "$VENV_DIR/"* ]]; then
        rm -f "$link"
    fi
done
rm -rf "$VENV_DIR"

if ((PURGE_STATE)); then
    if [[ -n "${CODEX_WATCH_STATE_DIR:-}" ]]; then
        STATE_DIR="$CODEX_WATCH_STATE_DIR"
    elif [[ -n "${XDG_STATE_HOME:-}" ]]; then
        STATE_DIR="$XDG_STATE_HOME/codex-goal-watchdog"
    else
        STATE_DIR="$HOME/.local/state/codex-goal-watchdog"
    fi
    rm -rf "$STATE_DIR"
fi

printf 'Removed codex-goal-watchdog.\n'
if ((!PURGE_STATE)); then
    printf 'State and logs were retained. Use --purge-state to remove them.\n'
fi
