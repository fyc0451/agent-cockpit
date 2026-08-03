#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LABEL="io.github.fyc0451.agent-cockpit"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"
PLIST_TEMPLATE="$INSTALL_DIR/agent-cockpit.plist"

if [[ ! "$INSTALL_DIR" =~ ^/[[:alnum:]_./-]+$ ]]; then
  echo "安装路径包含 launchd 模板不支持的字符: $INSTALL_DIR" >&2
  exit 1
fi

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
  local pid cwd command
  command -v lsof >/dev/null 2>&1 || return 0
  while IFS= read -r pid; do
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1 || true)"
    command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    if [[ "$cwd" != "$INSTALL_DIR" || "$command" != *server.py* ]]; then
      echo "端口 $COCKPIT_PORT 已被其他进程占用(pid=$pid)，未自动终止。" >&2
      exit 1
    fi
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
  mkdir -p "$PLIST_DIR"
  sed "s|__INSTALL_DIR__|$INSTALL_DIR|g" "$PLIST_TEMPLATE" > "$PLIST_PATH"
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

case "${1:-}" in
  run)
    load_runtime_env
    export PYTHONUNBUFFERED=1
    export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.opencode/bin:$HOME/.kimi-code/bin:/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"
    exec "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/server.py"
    ;;
  install|restart)
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
