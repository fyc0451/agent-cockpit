#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${AGENT_COCKPIT_REPO:-https://github.com/fyc0451/agent-cockpit.git}"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if git -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  && [[ -f "$SCRIPT_DIR/server.py" ]]; then
  INSTALL_DIR="$SCRIPT_DIR"
else
  INSTALL_DIR="${AGENT_COCKPIT_DIR:-$HOME/agent-cockpit}"
fi
# 安装目录允许空格/中文等正常路径字符。systemd ExecStart 不允许控制字符，
# 因此这里只拒绝相对路径与控制字符，不再使用字符白名单。
if [[ "$INSTALL_DIR" != /* || "$INSTALL_DIR" =~ [[:cntrl:]] ]]; then
  echo "安装路径必须是绝对路径且不能包含控制字符: $INSTALL_DIR" >&2
  exit 1
fi
if [[ -e "$INSTALL_DIR" ]] \
  && ! git -C "$INSTALL_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "安装目录已存在且不是 git 仓库: $INSTALL_DIR" >&2
  exit 1
fi
if ! git -C "$INSTALL_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

# curl 直装时 helper 要等 clone 后才存在。
# shellcheck source=install-paths.sh
source "$INSTALL_DIR/install-paths.sh"

PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 12):
    raise SystemExit("Agent Cockpit requires Python 3.12+")
PY

"$PYTHON_BIN" -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
"$INSTALL_DIR/install-agent-mail-tools.sh" "$INSTALL_DIR"

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  if [[ ! -f "$INSTALL_DIR/.env.example" ]]; then
    echo "未找到 $INSTALL_DIR/.env.example，无法创建配置文件" >&2
    exit 1
  fi
  cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
  chmod 600 "$INSTALL_DIR/.env"
fi

if command -v loginctl >/dev/null 2>&1; then
  if ! loginctl enable-linger "$(id -un)" >/dev/null 2>&1; then
    echo '警告: 无法启用 lingering；用户登出后服务会停止。请手动运行: loginctl enable-linger $USER' >&2
  fi
fi

if command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload; then
  UNIT_DIR="$HOME/.config/systemd/user"
  UNIT_PATH="$UNIT_DIR/agent-cockpit.service"
  mkdir -p "$UNIT_DIR"
  SYSTEMD_INSTALL_DIR="$(ac_escape_systemd_value "$INSTALL_DIR")"
  SYSTEMD_EXEC_DIR="$(ac_escape_systemd_exec_value "$INSTALL_DIR")"
  sed \
    -e "s|__INSTALL_EXEC_DIR__|$(ac_escape_sed_replacement "$SYSTEMD_EXEC_DIR")|g" \
    -e "s|__INSTALL_DIR__|$(ac_escape_sed_replacement "$SYSTEMD_INSTALL_DIR")|g" \
    "$INSTALL_DIR/agent-cockpit.service" > "$UNIT_PATH"
  systemctl --user daemon-reload
  systemctl --user disable --now agent-mail-dashboard.service >/dev/null 2>&1 || true
  systemctl --user enable --now agent-cockpit.service
  echo "Agent Cockpit 已启动: http://127.0.0.1:8790"
elif [[ "$(uname -s)" == "Darwin" ]] && command -v launchctl >/dev/null 2>&1; then
  "$INSTALL_DIR/launchd.sh" install
  echo "Agent Cockpit 已作为 macOS LaunchAgent 启动: http://127.0.0.1:8790"
else
  echo "安装完成，但当前环境没有可用的 systemd 或 launchd user service。" >&2
  echo "请运行: $INSTALL_DIR/.venv/bin/python $INSTALL_DIR/server.py" >&2
fi

echo "配置文件: $INSTALL_DIR/.env"
echo "自检命令: $INSTALL_DIR/doctor.sh"
