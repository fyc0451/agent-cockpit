#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=install-paths.sh
source "$INSTALL_DIR/install-paths.sh"
ac_validate_install_dir "$INSTALL_DIR"
if ! git -C "$INSTALL_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
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
# 升级时顺带自检本地 Hub：已有可用 Hub 直接复用，缺失/损坏按托管配置自愈。
"$INSTALL_DIR/install-agent-mail-hub.sh"

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
  # enable 仅设置开机自启；restart 确保已 active 实例加载新代码，未启动则等价 start。
  systemctl --user enable agent-cockpit.service
  systemctl --user restart agent-cockpit.service
  echo "升级完成，Agent Cockpit 已重启。Herdr session 不受影响。"
elif [[ "$(uname -s)" == "Darwin" ]] && command -v launchctl >/dev/null 2>&1; then
  "$INSTALL_DIR/launchd.sh" restart
  echo "升级完成，macOS LaunchAgent 已重启。Herdr session 不受影响。"
else
  echo "代码和依赖已升级；请手动重启 Agent Cockpit。" >&2
fi
