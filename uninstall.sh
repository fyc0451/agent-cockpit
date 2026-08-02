#!/usr/bin/env bash
set -euo pipefail

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user disable --now agent-cockpit.service >/dev/null 2>&1 || true
  systemctl --user disable --now agent-mail-dashboard.service >/dev/null 2>&1 || true
fi
rm -f -- "$HOME/.config/systemd/user/agent-cockpit.service"
rm -f -- "$HOME/.config/systemd/user/agent-mail-dashboard.service"
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload >/dev/null 2>&1 || true
fi

echo "Agent Cockpit 服务已卸载。"
echo "为避免误删，代码、.env、~/dashboard-data 和 ~/dashboard-uploads 均已保留。"
