#!/usr/bin/env bash
set -uo pipefail

INSTALL_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
FAILURES=0
WARNINGS=0

ok() { printf '✓ %s\n' "$1"; }
fail() { printf '✗ %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
warn() { printf '! %s\n' "$1"; WARNINGS=$((WARNINGS + 1)); }

ENV_FILE="${AGENT_COCKPIT_ENV:-$INSTALL_DIR/.env}"

env_value() {
  local name="$1" value first last
  [[ -f "$ENV_FILE" ]] || return 0
  value="$(sed -n -E "s/^[[:space:]]*${name}=//p" "$ENV_FILE" | tail -n 1)"
  if [[ ${#value} -ge 2 ]]; then
    first="${value:0:1}"
    last="${value: -1}"
    if [[ "$first" == "$last" && ( "$first" == '"' || "$first" == "'" ) ]]; then
      value="${value:1:${#value}-2}"
    fi
  fi
  printf '%s' "$value"
}

HERDR_BIN_VALUE="$(env_value HERDR_BIN)"
CODEX_BIN_VALUE="$(env_value CODEX_BIN)"
HOST_VALUE="$(env_value COCKPIT_HOST)"
TOKEN_VALUE="$(env_value COCKPIT_TOKEN)"

CHECK_PYTHON="$INSTALL_DIR/.venv/bin/python"
if [[ ! -x "$CHECK_PYTHON" ]]; then
  CHECK_PYTHON="$(command -v python3 2>/dev/null || true)"
fi
if [[ -n "$CHECK_PYTHON" ]] && "$CHECK_PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'; then
  ok "Python 3.12+"
else
  fail "需要 Python 3.12+"
fi

command -v git >/dev/null 2>&1 && ok "git 可用" || fail "未找到 git"

if [[ -x "$INSTALL_DIR/.venv/bin/python" ]] && \
   "$INSTALL_DIR/.venv/bin/python" -c 'import fastapi, httpx, sse_starlette, uvicorn' >/dev/null 2>&1; then
  ok "Python 虚拟环境和运行依赖可用"
else
  fail "虚拟环境或依赖缺失；运行 ./install.sh"
fi

if command -v herdr >/dev/null 2>&1 || [[ -x "$HOME/.local/bin/herdr" ]] || \
   [[ -n "$HERDR_BIN_VALUE" && -x "$HERDR_BIN_VALUE" ]]; then
  ok "herdr 可用"
else
  fail "未找到 herdr；也可在 .env 设置 HERDR_BIN"
fi

if command -v codex >/dev/null 2>&1 || \
   [[ -n "$CODEX_BIN_VALUE" && -x "$CODEX_BIN_VALUE" ]]; then
  ok "codex 可用"
else
  warn "未找到 codex；后台 codex task 功能不可用"
fi

[[ -f "$HOME/mcp_agent_mail/storage.sqlite3" ]] \
  && ok "Agent Mail 数据库存在" \
  || fail "缺少 ~/mcp_agent_mail/storage.sqlite3"
[[ -f "$HOME/.agent-mail/client.env" ]] \
  && ok "Agent Mail 客户端配置存在" \
  || warn "缺少 ~/.agent-mail/client.env；发信/确认功能可能不可用"

if [[ -f "$ENV_FILE" ]]; then
  ok ".env 存在"
  if grep -Eq '^[[:space:]]*export[[:space:]]+(COCKPIT_HOST|COCKPIT_TOKEN|HERDR_BIN|CODEX_BIN)=' "$ENV_FILE"; then
    fail ".env 不能使用 export；systemd EnvironmentFile 只接受 KEY=VALUE"
  fi
  if [[ -n "$HOST_VALUE" && "$HOST_VALUE" != "127.0.0.1" && \
        "$HOST_VALUE" != "localhost" && "$HOST_VALUE" != "::1" ]]; then
    [[ -n "$TOKEN_VALUE" ]] \
      && ok "非回环监听已配置 Token（未显示内容）" \
      || fail "非回环监听必须设置 COCKPIT_TOKEN"
    warn "非回环访问请使用 HTTPS 或 Tailscale Serve"
  fi
else
  warn ".env 不存在；将使用仅本机可访问的默认配置"
fi

if command -v systemctl >/dev/null 2>&1 && \
   systemctl --user is-active --quiet agent-cockpit.service 2>/dev/null; then
  ok "agent-cockpit.service 正在运行"
else
  warn "agent-cockpit.service 未运行或 systemd user bus 不可用"
fi

printf '\n结果: %d 个错误，%d 个警告。\n' "$FAILURES" "$WARNINGS"
exit "$FAILURES"
