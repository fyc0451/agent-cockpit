#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${1:-$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
HERDR_INSTALL_URL="${HERDR_INSTALL_URL:-https://herdr.dev/install.sh}"
HERDR_INSTALL_DIR="${HERDR_INSTALL_DIR:-$HOME/.local/bin}"

resolve_herdr() {
  if [[ -n "${HERDR_BIN:-}" && -x "${HERDR_BIN}" ]]; then
    printf '%s\n' "$HERDR_BIN"
  elif command -v herdr >/dev/null 2>&1; then
    command -v herdr
  elif [[ -x "$HERDR_INSTALL_DIR/herdr" ]]; then
    printf '%s\n' "$HERDR_INSTALL_DIR/herdr"
  fi
}

herdr_bin="$(resolve_herdr)"
if [[ -z "$herdr_bin" ]]; then
  if ! command -v curl >/dev/null 2>&1; then
    echo "缺少 curl，无法自动安装 Herdr" >&2
    exit 1
  fi
  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/agent-cockpit-herdr.XXXXXX")"
  trap 'rm -rf -- "$tmp_dir"' EXIT
  installer="$tmp_dir/install.sh"
  curl -fsSL --retry 3 --connect-timeout 10 --max-time 60 \
    "$HERDR_INSTALL_URL" -o "$installer"
  HERDR_INSTALL_DIR="$HERDR_INSTALL_DIR" sh "$installer"
  herdr_bin="$(resolve_herdr)"
  if [[ -z "$herdr_bin" ]]; then
    echo "Herdr 安装器执行完成，但未找到可执行文件" >&2
    exit 1
  fi
  echo "Herdr 已自动安装: $herdr_bin"
else
  echo "Herdr 已安装: $herdr_bin"
fi

python_bin="$INSTALL_DIR/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  echo "缺少 Cockpit Python 环境，无法检测 Agent CLI: $python_bin" >&2
  exit 1
fi

detected_agents=()
while IFS= read -r agent; do
  [[ -n "$agent" ]] && detected_agents+=("$agent")
done < <(
  PYTHONPATH="$INSTALL_DIR" "$python_bin" - <<'PY'
from agent_cockpit import herdr_client, settings

for name in herdr_client.installed_agent_bins(settings.KNOWN_AGENTS):
    print(name)
PY
)

if [[ ${#detected_agents[@]} -eq 0 ]]; then
  echo "未检测到 Agent CLI；Agent CLI 不由 Cockpit 安装，安装后重新运行 install.sh/upgrade.sh 即可。"
  exit 0
fi

echo "检测到 Agent CLI: ${detected_agents[*]}（Agent CLI 不由 Cockpit 安装）"
integration_failed=0
for agent in "${detected_agents[@]}"; do
  if "$herdr_bin" integration install "$agent"; then
    echo "已安装 Herdr 集成: $agent"
  else
    echo "警告: Herdr 集成安装失败，Cockpit 仍可启动: $agent" >&2
    integration_failed=1
  fi
done

if [[ $integration_failed -ne 0 ]]; then
  echo "部分 Herdr 集成未安装成功；请修复对应 CLI 配置后重新运行 upgrade.sh。" >&2
fi
