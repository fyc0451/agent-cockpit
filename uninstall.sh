#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user disable --now agent-cockpit.service >/dev/null 2>&1 || true
  systemctl --user disable --now agent-mail-dashboard.service >/dev/null 2>&1 || true
fi
rm -f -- "$HOME/.config/systemd/user/agent-cockpit.service"
rm -f -- "$HOME/.config/systemd/user/agent-mail-dashboard.service"
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload >/dev/null 2>&1 || true
fi
if [[ "$(uname -s)" == "Darwin" ]] && command -v launchctl >/dev/null 2>&1; then
  "$INSTALL_DIR/launchd.sh" uninstall
fi

echo "Agent Cockpit 服务已卸载。"
echo "为避免误删，代码、.env、~/dashboard-data 和 ~/dashboard-uploads 均已保留。"
