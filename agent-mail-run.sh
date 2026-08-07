#!/usr/bin/env bash
# 启动本地 Agent Mail Hub（Linux systemd 入口；macOS 用 agent-mail-launchd.sh）。
# 监听地址/端口与 token 全部从 client.env 严格解析（loopback 限定），不落日志；
# client.env 指向远程 Hub 时拒绝启动本地进程。数据目录兼容旧版 ~/mcp_agent_mail。
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=install-paths.sh
source "$SCRIPT_DIR/install-paths.sh"

CLIENT_ENV="${AGENT_MAIL_CLIENT_ENV:-$HOME/.agent-mail/client.env}"
LEGACY_DIR="$HOME/mcp_agent_mail"
DEFAULT_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/mcp_agent_mail"
if [[ -n "${MCP_AGENT_MAIL_DIR:-}" ]]; then
  REPO_DIR="$MCP_AGENT_MAIL_DIR"
elif [[ -x "$LEGACY_DIR/.venv/bin/python" ]]; then
  REPO_DIR="$LEGACY_DIR"
else
  REPO_DIR="$DEFAULT_DIR"
fi

ac_validate_install_dir "$REPO_DIR"

if [[ ! -f "$CLIENT_ENV" ]]; then
  echo "缺少 Agent Mail 客户端配置: $CLIENT_ENV" >&2
  exit 1
fi
TOKEN="$(sed -n 's/^token=//p' "$CLIENT_ENV" | tail -n 1)"
if [[ -z "$TOKEN" ]]; then
  echo "Agent Mail token 未配置: $CLIENT_ENV" >&2
  exit 1
fi
if ! read -r HTTP_HOST HTTP_PORT < <(ac_client_env_loopback_hub "$CLIENT_ENV"); then
  echo "client.env 的 hub 不是 loopback HTTP 地址，本地 runner 不启动: $CLIENT_ENV" >&2
  exit 1
fi
if [[ ! -x "$REPO_DIR/.venv/bin/python" ]]; then
  echo "缺少 Agent Mail 虚拟环境: $REPO_DIR/.venv" >&2
  exit 1
fi

export PYTHONUNBUFFERED=1
export HTTP_HOST HTTP_PORT HTTP_BEARER_TOKEN="$TOKEN"
export DATABASE_URL="sqlite+aiosqlite:///$REPO_DIR/storage.sqlite3"
export STORAGE_ROOT="${MCP_AGENT_MAIL_STORAGE_ROOT:-${XDG_STATE_HOME:-$HOME/.local/state}/mcp-agent-mail/mailbox}"
cd "$REPO_DIR"
exec "$REPO_DIR/.venv/bin/python" -m mcp_agent_mail.cli serve-http
