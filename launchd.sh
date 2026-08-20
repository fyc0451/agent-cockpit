#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LABEL="io.github.fyc0451.agent-cockpit"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"
PLIST_TEMPLATE="$INSTALL_DIR/agent-cockpit.plist"

# shellcheck source=install-paths.sh
source "$INSTALL_DIR/install-paths.sh"
ac_validate_install_dir "$INSTALL_DIR"

load_runtime_env() {
  COCKPIT_HOST=127.0.0.1
  COCKPIT_PORT=8790
  if [[ -f "$INSTALL_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$INSTALL_DIR/.env"
    set +a
  fi
  if [[ ! "$COCKPIT_PORT" =~ ^[0-9]+$ ]]; then
    echo "COCKPIT_PORT 必须是数字: $COCKPIT_PORT" >&2
    exit 1
  fi
}

stop_legacy_listener() {
  local mode="${1:-stop}" pid cwd command
  command -v lsof >/dev/null 2>&1 || return 0
  while IFS= read -r pid; do
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1 || true)"
    command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    if [[ "$cwd" != "$INSTALL_DIR" || ( "$command" != *server.py* && "$command" != *dev_server.py* ) ]]; then
      echo "端口 $COCKPIT_PORT 已被其他进程占用(pid=$pid)，未自动终止。" >&2
      exit 1
    fi
    [[ "$mode" == "check" ]] && continue
    echo "正在停止旧版 Agent Cockpit(pid=$pid)..."
    kill "$pid"
    for _ in {1..25}; do
      if ! kill -0 "$pid" 2>/dev/null; then
        break
      fi
      sleep 0.2
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "旧进程未正常退出，请手动停止 pid=$pid 后重试。" >&2
      exit 1
    fi
  done < <(lsof -nP -tiTCP:"$COCKPIT_PORT" -sTCP:LISTEN 2>/dev/null || true)
}

install_service() {
  if [[ ! -f "$PLIST_TEMPLATE" ]]; then
    echo "缺少 launchd 模板: $PLIST_TEMPLATE" >&2
    exit 1
  fi
  load_runtime_env
  # 先确认端口没有无关进程，再卸载现有服务；失败时保留可用的 LaunchAgent。
  stop_legacy_listener check
  mkdir -p "$PLIST_DIR"
  PLIST_INSTALL_DIR="$(ac_escape_plist_value "$INSTALL_DIR")"
  sed "s|__INSTALL_DIR__|$(ac_escape_sed_replacement "$PLIST_INSTALL_DIR")|g" "$PLIST_TEMPLATE" > "$PLIST_PATH"
  chmod 600 "$PLIST_PATH"
  launchctl bootout "$SERVICE" >/dev/null 2>&1 || true
  stop_legacy_listener
  launchctl bootstrap "$DOMAIN" "$PLIST_PATH"
  launchctl enable "$SERVICE"
  launchctl kickstart -k "$SERVICE"
  echo "Agent Cockpit LaunchAgent 已启动: $SERVICE"
}

uninstall_service() {
  launchctl bootout "$SERVICE" >/dev/null 2>&1 || true
  rm -f -- "$PLIST_PATH"
  echo "Agent Cockpit LaunchAgent 已卸载。"
}

prepare_logs() {
  # Shell umask for any non-launchd file creation; plist Umask covers Standard*Path.
  umask 077
  if [[ -L "$INSTALL_DIR/logs" ]]; then
    echo "日志目录不得为符号链接: $INSTALL_DIR/logs" >&2
    exit 1
  fi
  if [[ -x "$INSTALL_DIR/.venv/bin/python" ]]; then
    # Prefer Python: full parent-chain checks + launchd bootstrap rotation.
    # Pass absolute literal only (no Path.resolve — would hide install symlink).
    "$INSTALL_DIR/.venv/bin/python" - "$INSTALL_DIR" <<'PY'
import sys
from pathlib import Path

raw = sys.argv[1]
install = Path(raw)
if not install.is_absolute():
    print(f"install_dir 必须是绝对路径: {raw!r}", file=sys.stderr)
    raise SystemExit(2)
if install.is_symlink():
    print(f"install_dir 不得为符号链接: {raw}", file=sys.stderr)
    raise SystemExit(2)
# Literal path only — do not resolve(); chain checks see real symlink names.
sys.path.insert(0, raw)
from agent_cockpit.log_config import LogConfigError, prepare_macos_log_dir

try:
    prepare_macos_log_dir(install)
except LogConfigError as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(2) from exc
except Exception as exc:
    print(f"prepare_macos_log_dir failed: {exc}", file=sys.stderr)
    raise SystemExit(2) from exc
PY
  else
    # Pre-venv / fixture: only create if absent (never mkdir through a symlink).
    if [[ ! -e "$INSTALL_DIR/logs" ]]; then
      mkdir -m 700 "$INSTALL_DIR/logs" || exit 1
    elif [[ ! -d "$INSTALL_DIR/logs" ]]; then
      echo "日志路径不是目录: $INSTALL_DIR/logs" >&2
      exit 1
    fi
  fi
}

case "${1:-}" in
  run)
    load_runtime_env
    prepare_logs
    export PYTHONUNBUFFERED=1
    export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.opencode/bin:$HOME/.kimi-code/bin:/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"
    exec "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/scripts/dev_server.py"
    ;;
  install|restart)
    prepare_logs
    install_service
    ;;
  uninstall)
    uninstall_service
    ;;
  *)
    echo "用法: $0 {run|install|restart|uninstall}" >&2
    exit 2
    ;;
esac
