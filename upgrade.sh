#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! "$INSTALL_DIR" =~ ^/[[:alnum:]_./-]+$ ]]; then
  echo "安装路径包含 systemd 模板不支持的字符: $INSTALL_DIR" >&2
  exit 1
fi
if [[ ! -d "$INSTALL_DIR/.git" ]]; then
  echo "不是 git 安装目录: $INSTALL_DIR" >&2
  exit 1
fi
if ! git -C "$INSTALL_DIR" diff --quiet || ! git -C "$INSTALL_DIR" diff --cached --quiet; then
  echo "工作区有未提交修改，已停止升级。请先提交或暂存。" >&2
  exit 1
fi

if ! git -C "$INSTALL_DIR" rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
  echo "当前分支没有上游，无法自动升级。" >&2
  exit 1
fi
if [[ ! -x "$INSTALL_DIR/.venv/bin/pip" ]]; then
  echo "未找到虚拟环境，请先运行 install.sh。" >&2
  exit 1
fi
git -C "$INSTALL_DIR" pull --ff-only
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
"$INSTALL_DIR/install-agent-mail-tools.sh" "$INSTALL_DIR"

if command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload; then
  UNIT_DIR="$HOME/.config/systemd/user"
  UNIT_PATH="$UNIT_DIR/agent-cockpit.service"
  mkdir -p "$UNIT_DIR"
  sed "s|%h/agent-cockpit|$INSTALL_DIR|g" \
    "$INSTALL_DIR/agent-cockpit.service" > "$UNIT_PATH"
  systemctl --user daemon-reload
  systemctl --user disable --now agent-mail-dashboard.service >/dev/null 2>&1 || true
  systemctl --user enable agent-cockpit.service
  # 升级时服务通常已在运行;enable --now 只启动未运行的实例,不会让已运行的
  # 进程加载新代码,必须显式 restart 才能让本次升级生效。
  systemctl --user restart agent-cockpit.service
  echo "升级完成，Agent Cockpit 已重启。Herdr session 不受影响。"
elif [[ "$(uname -s)" == "Darwin" ]] && command -v launchctl >/dev/null 2>&1; then
  "$INSTALL_DIR/launchd.sh" restart
  echo "升级完成，macOS LaunchAgent 已重启。Herdr session 不受影响。"
else
  echo "代码和依赖已升级；请手动重启 Agent Cockpit。" >&2
fi
