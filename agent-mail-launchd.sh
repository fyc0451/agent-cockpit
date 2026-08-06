#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${MCP_AGENT_MAIL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/mcp_agent_mail}"
CLIENT_ENV="${AGENT_MAIL_CLIENT_ENV:-$HOME/.agent-mail/client.env}"
LABEL="io.github.fyc0451.mcp-agent-mail-local"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"
PLIST_TEMPLATE="$INSTALL_DIR/agent-mail.plist"

validate_paths() {
  if [[ ! "$INSTALL_DIR" =~ ^/[[:alnum:]_./-]+$ \
    || ! "$REPO_DIR" =~ ^/[[:alnum:]_./-]+$ ]]; then
    echo "Agent Mail 安装路径仅允许字母、数字、点、下划线、斜杠和连字符。" >&2
    exit 1
  fi
}

load_token() {
  if [[ ! -f "$CLIENT_ENV" ]]; then
    echo "缺少 Agent Mail 客户端配置: $CLIENT_ENV" >&2
    exit 1
  fi
  local token
  token="$(sed -n 's/^token=//p' "$CLIENT_ENV" | tail -n 1)"
  if [[ -z "$token" ]]; then
    echo "Agent Mail token 未配置: $CLIENT_ENV" >&2
    exit 1
  fi
  printf '%s' "$token"
}

validate_runtime() {
  validate_paths
  if [[ ! -x "$REPO_DIR/.venv/bin/python" ]]; then
    echo "缺少 Agent Mail 虚拟环境: $REPO_DIR/.venv" >&2
    exit 1
  fi
  if [[ ! -f "$PLIST_TEMPLATE" ]]; then
    echo "缺少 launchd 模板: $PLIST_TEMPLATE" >&2
    exit 1
  fi
  load_token >/dev/null
}

run_server() {
  validate_runtime
  export PYTHONUNBUFFERED=1
  export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
  export HTTP_HOST=127.0.0.1
  export HTTP_PORT=8765
  export HTTP_BEARER_TOKEN="$(load_token)"
  export DATABASE_URL="sqlite+aiosqlite:///$REPO_DIR/storage.sqlite3"
  export STORAGE_ROOT="${MCP_AGENT_MAIL_STORAGE_ROOT:-${XDG_STATE_HOME:-$HOME/.local/state}/mcp-agent-mail/mailbox}"
  cd "$REPO_DIR"
  exec "$REPO_DIR/.venv/bin/python" -m mcp_agent_mail.cli serve-http
}

install_service() {
  validate_runtime
  mkdir -p "$PLIST_DIR"
  sed -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
    -e "s|__REPO_DIR__|$REPO_DIR|g" \
    "$PLIST_TEMPLATE" > "$PLIST_PATH"
  chmod 600 "$PLIST_PATH"

  launchctl bootout "$SERVICE" >/dev/null 2>&1 || true
  # launchctl 返回时旧进程可能仍在退出；给它一个有限的优雅释放窗口，
  # 避免 restart 把自己的旧监听误判成非托管进程。
  if command -v lsof >/dev/null 2>&1; then
    for _ in {1..25}; do
      if ! lsof -nP -tiTCP:8765 -sTCP:LISTEN 2>/dev/null | grep -q .; then
        break
      fi
      sleep 0.2
    done
  fi
  if command -v lsof >/dev/null 2>&1 \
    && lsof -nP -tiTCP:8765 -sTCP:LISTEN 2>/dev/null | grep -q .; then
    echo "端口 8765 已被非托管进程占用，未启动 Agent Mail LaunchAgent。" >&2
    exit 1
  fi
  launchctl bootstrap "$DOMAIN" "$PLIST_PATH"
  launchctl enable "$SERVICE"
  launchctl kickstart -k "$SERVICE"
  echo "本地 Agent Mail LaunchAgent 已启动: $SERVICE"
}

uninstall_service() {
  launchctl bootout "$SERVICE" >/dev/null 2>&1 || true
  rm -f -- "$PLIST_PATH"
  echo "本地 Agent Mail LaunchAgent 已卸载；仓库和数据均已保留。"
}

case "${1:-}" in
  run) run_server ;;
  install|restart) install_service ;;
  uninstall) uninstall_service ;;
  status) launchctl print "$SERVICE" ;;
  *) echo "用法: $0 {run|install|restart|uninstall|status}" >&2; exit 2 ;;
esac
