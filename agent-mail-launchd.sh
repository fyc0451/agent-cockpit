#!/usr/bin/env bash
# 本地 Agent Mail Hub 的 macOS LaunchAgent 管理与运行入口。
# 监听端口/token 从 client.env 严格解析；plist 渲染复用 install-paths.sh 转义。
set -euo pipefail

INSTALL_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=install-paths.sh
source "$INSTALL_DIR/install-paths.sh"

REPO_DIR="${MCP_AGENT_MAIL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/mcp_agent_mail}"
CLIENT_ENV="${AGENT_MAIL_CLIENT_ENV:-$HOME/.agent-mail/client.env}"
LABEL="io.github.fyc0451.mcp-agent-mail-local"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"
PLIST_TEMPLATE="$INSTALL_DIR/agent-mail.plist"

validate_paths() {
  ac_validate_install_dir "$INSTALL_DIR" || exit 1
  ac_validate_install_dir "$REPO_DIR" || exit 1
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

load_hub_endpoint() {
  # 输出 "HOST PORT"；client.env 指向远程 hub 时拒绝。
  ac_client_env_loopback_hub "$CLIENT_ENV" || {
    echo "client.env 的 hub 不是 loopback HTTP 地址，本地 runner 不启动: $CLIENT_ENV" >&2
    exit 1
  }
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
  load_hub_endpoint >/dev/null
}

run_server() {
  validate_runtime
  local hub_host hub_port
  read -r hub_host hub_port < <(load_hub_endpoint)
  export PYTHONUNBUFFERED=1
  export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
  export HTTP_HOST="$hub_host"
  export HTTP_PORT="$hub_port"
  export HTTP_BEARER_TOKEN="$(load_token)"
  export DATABASE_URL="sqlite+aiosqlite:///$REPO_DIR/storage.sqlite3"
  export STORAGE_ROOT="${MCP_AGENT_MAIL_STORAGE_ROOT:-${XDG_STATE_HOME:-$HOME/.local/state}/mcp-agent-mail/mailbox}"
  cd "$REPO_DIR"
  exec "$REPO_DIR/.venv/bin/python" -m mcp_agent_mail.cli serve-http
}

render_plist() {
  local plist_install plist_repo
  plist_install="$(ac_escape_plist_value "$INSTALL_DIR")"
  plist_repo="$(ac_escape_plist_value "$REPO_DIR")"
  sed \
    -e "s|__INSTALL_DIR__|$(ac_escape_sed_replacement "$plist_install")|g" \
    -e "s|__REPO_DIR__|$(ac_escape_sed_replacement "$plist_repo")|g" \
    "$PLIST_TEMPLATE" > "$PLIST_PATH"
  chmod 600 "$PLIST_PATH"
}

install_service() {
  validate_runtime
  local hub_port
  read -r _ hub_port < <(load_hub_endpoint)
  mkdir -p "$PLIST_DIR"
  render_plist

  launchctl bootout "$SERVICE" >/dev/null 2>&1 || true
  # launchctl 返回时旧进程可能仍在退出；给它一个有限的优雅释放窗口，
  # 避免 restart 把自己的旧监听误判成非托管进程。
  if command -v lsof >/dev/null 2>&1; then
    for _ in {1..25}; do
      if ! lsof -nP -tiTCP:"$hub_port" -sTCP:LISTEN 2>/dev/null | grep -q .; then
        break
      fi
      sleep 0.2
    done
  fi
  if command -v lsof >/dev/null 2>&1 \
    && lsof -nP -tiTCP:"$hub_port" -sTCP:LISTEN 2>/dev/null | grep -q .; then
    echo "端口 $hub_port 已被非托管进程占用，未启动 Agent Mail LaunchAgent。" >&2
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
  render-plist) validate_paths; render_plist ;;
  uninstall) uninstall_service ;;
  status) launchctl print "$SERVICE" ;;
  *) echo "用法: $0 {run|install|restart|uninstall|status}" >&2; exit 2 ;;
esac
