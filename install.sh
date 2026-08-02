#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${AGENT_COCKPIT_REPO:-https://github.com/fyc0451/agent-cockpit.git}"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ -d "$SCRIPT_DIR/.git" && -f "$SCRIPT_DIR/server.py" ]]; then
  INSTALL_DIR="$SCRIPT_DIR"
else
  INSTALL_DIR="${AGENT_COCKPIT_DIR:-$HOME/agent-cockpit}"
fi
if [[ ! "$INSTALL_DIR" =~ ^/[[:alnum:]_./-]+$ ]]; then
  echo "安装路径仅允许字母、数字、点、下划线、斜杠和连字符: $INSTALL_DIR" >&2
  exit 1
fi
if [[ -e "$INSTALL_DIR" && ! -d "$INSTALL_DIR/.git" ]]; then
  echo "安装目录已存在且不是 git 仓库: $INSTALL_DIR" >&2
  exit 1
fi
if [[ ! -d "$INSTALL_DIR/.git" ]]; then
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 12):
    raise SystemExit("Agent Cockpit requires Python 3.12+")
PY

"$PYTHON_BIN" -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  if [[ ! -f "$INSTALL_DIR/.env.example" ]]; then
    echo "未找到 $INSTALL_DIR/.env.example，无法创建配置文件" >&2
    exit 1
  fi
  cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
  chmod 600 "$INSTALL_DIR/.env"
fi

UNIT_DIR="$HOME/.config/systemd/user"
UNIT_PATH="$UNIT_DIR/agent-cockpit.service"
mkdir -p "$UNIT_DIR"
sed "s|%h/agent-cockpit|$INSTALL_DIR|g" \
  "$INSTALL_DIR/agent-cockpit.service" > "$UNIT_PATH"

if command -v loginctl >/dev/null 2>&1; then
  if ! loginctl enable-linger "$(id -un)" >/dev/null 2>&1; then
    echo '警告: 无法启用 lingering；用户登出后服务会停止。请手动运行: loginctl enable-linger $USER' >&2
  fi
fi

if command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload; then
  systemctl --user disable --now agent-mail-dashboard.service >/dev/null 2>&1 || true
  systemctl --user enable --now agent-cockpit.service
  echo "Agent Cockpit 已启动: http://127.0.0.1:8790"
else
  echo "安装完成，但当前环境没有可用的 systemd user bus。" >&2
  echo "请运行: $INSTALL_DIR/.venv/bin/python $INSTALL_DIR/server.py" >&2
fi

echo "配置文件: $INSTALL_DIR/.env"
echo "自检命令: $INSTALL_DIR/doctor.sh"
