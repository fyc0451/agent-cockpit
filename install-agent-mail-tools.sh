#!/usr/bin/env bash
set -u

INSTALL_DIR="${1:-$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"

for name in am-register am-retire am-init-project mail-send mail-recv mail-identity-inject mail-hook-check task-report team-work project-consult; do
  source="$INSTALL_DIR/agent-mail-tools/$name"
  target="$BIN_DIR/$name"
  if [[ ! -x "$source" ]]; then
    echo "警告: Agent Mail 工具不可执行: $source" >&2
    continue
  fi
  if [[ -f "$target" && ! -L "$target" ]]; then
    echo "警告: 保留已有普通文件，不覆盖: $target" >&2
    continue
  fi
  if [[ -e "$target" && ! -L "$target" ]]; then
    echo "警告: 保留已有路径，不覆盖: $target" >&2
    continue
  fi
  if [[ -L "$target" ]]; then
    current="$(readlink "$target")"
    case "$current" in
      "$source") continue ;;
      "$HOME/agent-mail-tools/$name"|"$HOME/agent-cockpit/agent-mail-tools/$name"|"$INSTALL_DIR/agent-mail-tools/$name")
        if ln -sfn "$source" "$target"; then
          echo "已更新旧版 Agent Mail 工具软链: $target"
        else
          echo "警告: 无法更新 Agent Mail 工具软链: $target" >&2
        fi
        ;;
      *) echo "警告: 保留用户已有软链，不覆盖: $target -> $current" >&2 ;;
    esac
    continue
  fi
  if ! ln -s "$source" "$target"; then
    echo "警告: 无法创建 Agent Mail 工具软链: $target" >&2
  fi
done

# 早期 SessionStart 配置固定调用此路径。仅迁移我们能识别的旧 helper，
# 其他用户文件保持不动；这样升级后 Hook 会落到当前部署版本。
legacy_dir="$HOME/agent-mail-tools"
legacy_hook="$legacy_dir/mail-identity-inject"
source_hook="$INSTALL_DIR/agent-mail-tools/mail-identity-inject"
compat_hook="$BIN_DIR/mail-identity-inject"
resolved_compat="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' \
  "$compat_hook" 2>/dev/null || true)"
resolved_source="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' \
  "$source_hook" 2>/dev/null || true)"
if [[ -d "$legacy_dir" && -x "$source_hook" \
  && -n "$resolved_compat" && "$resolved_compat" == "$resolved_source" ]]; then
  if [[ -L "$legacy_hook" ]]; then
    current="$(readlink "$legacy_hook")"
    case "$current" in
      "$compat_hook") ;;
      "$source_hook"|"$HOME/agent-cockpit/agent-mail-tools/mail-identity-inject")
        ln -sfn "$compat_hook" "$legacy_hook" || \
          echo "警告: 无法更新旧 Hook 入口: $legacy_hook" >&2
        ;;
      *) echo "警告: 保留用户已有 Hook 软链: $legacy_hook -> $current" >&2 ;;
    esac
  elif [[ ! -e "$legacy_hook" ]]; then
    ln -s "$compat_hook" "$legacy_hook" || \
      echo "警告: 无法创建旧 Hook 兼容入口: $legacy_hook" >&2
  elif grep -q 'mail-identity-inject.*SessionStart hook' "$legacy_hook" 2>/dev/null; then
    backup="$legacy_hook.pre-cockpit"
    if [[ -e "$backup" || -L "$backup" ]]; then
      echo "警告: 旧 Hook 备份已存在，未迁移: $backup" >&2
    elif mv "$legacy_hook" "$backup"; then
      if ! ln -s "$compat_hook" "$legacy_hook"; then
        mv "$backup" "$legacy_hook"
        echo "警告: 无法创建旧 Hook 兼容入口，已恢复原文件" >&2
      else
        echo "已迁移旧 Hook 入口: ${legacy_hook}（备份: ${backup}）"
      fi
    fi
  else
    echo "警告: 保留用户已有 Hook 文件，不覆盖: $legacy_hook" >&2
  fi
fi

# 早期 UserPromptSubmit hook 固定调用 $HOME/agent-mail-tools/mail-hook-check（旧 bash 版）。
# 仅识别精确 SHA256 的旧版，备份后切到稳定软链；未知文件绝不覆盖，失败回滚，二次执行幂等。
legacy_hook_check="$legacy_dir/mail-hook-check"
compat_hook_check="$BIN_DIR/mail-hook-check"
source_hook_check="$INSTALL_DIR/agent-mail-tools/mail-hook-check"
legacy_hook_check_sha256="493f9bde787d8fa5fbadcee5690a436b9654d2c2f5be9861dff3203f4fddda7e"
hook_check_backup="$legacy_dir/mail-hook-check.pre-cockpit"
resolved_compat_hc="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' \
  "$compat_hook_check" 2>/dev/null || true)"
resolved_source_hc="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' \
  "$source_hook_check" 2>/dev/null || true)"
if [[ -d "$legacy_dir" && -x "$source_hook_check" \
  && -n "$resolved_compat_hc" && "$resolved_compat_hc" == "$resolved_source_hc" ]]; then
  if [[ -L "$legacy_hook_check" ]]; then
    current="$(readlink "$legacy_hook_check")"
    case "$current" in
      "$compat_hook_check") ;;
      "$source_hook_check"|"$HOME/agent-cockpit/agent-mail-tools/mail-hook-check")
        ln -sfn "$compat_hook_check" "$legacy_hook_check" || \
          echo "警告: 无法更新旧 mail-hook-check 软链: $legacy_hook_check" >&2
        ;;
      *) echo "警告: 保留用户已有 mail-hook-check 软链: $legacy_hook_check -> $current" >&2 ;;
    esac
  elif [[ ! -e "$legacy_hook_check" ]]; then
    ln -s "$compat_hook_check" "$legacy_hook_check" || \
      echo "警告: 无法创建旧 mail-hook-check 兼容入口: $legacy_hook_check" >&2
  elif [[ -f "$legacy_hook_check" ]]; then
    current_hash="$(python3 - "$legacy_hook_check" <<'PY' 2>/dev/null || true
import hashlib
import sys
from pathlib import Path
print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"
    if [[ "$current_hash" != "$legacy_hook_check_sha256" ]]; then
      echo "ACTIVATION_BLOCK: 保留未知 mail-hook-check 文件: $legacy_hook_check" >&2
    elif [[ -e "$hook_check_backup" || -L "$hook_check_backup" ]]; then
      echo "ACTIVATION_BLOCK: mail-hook-check rollback 备份已存在: $hook_check_backup" >&2
    elif mv "$legacy_hook_check" "$hook_check_backup"; then
      if ! ln -s "$compat_hook_check" "$legacy_hook_check"; then
        mv "$hook_check_backup" "$legacy_hook_check" 2>/dev/null || true
        echo "ACTIVATION_BLOCK: mail-hook-check 激活失败，已尝试回滚" >&2
      else
        echo "已迁移旧 mail-hook-check 入口: ${legacy_hook_check}（备份: ${hook_check_backup}）"
      fi
    else
      echo "ACTIVATION_BLOCK: 无法创建 mail-hook-check rollback 备份" >&2
    fi
  else
    echo "ACTIVATION_BLOCK: 保留用户已有 mail-hook-check 路径: $legacy_hook_check" >&2
  fi
fi

plugin_source="$INSTALL_DIR/agent-mail-tools/agent-mail.opencode-plugin.js"
plugin_dir="$HOME/.config/opencode/plugins"
plugin_target="$plugin_dir/agent-mail.js"
legacy_plugin_sha256="143e3a4bbd2e87d754cafb7125808acd2321e203352c1735950bc51e2ffebb22"
plugin_backup="$plugin_dir/agent-mail.js.pre-zcode-r5"
if [[ -f "$plugin_source" ]]; then
  mkdir -p "$plugin_dir"
  if [[ ! -e "$plugin_target" && ! -L "$plugin_target" ]]; then
    ln -s "$plugin_source" "$plugin_target" 2>/dev/null || \
      echo "警告: 无法创建 agent-mail 插件软链" >&2
  elif [[ -L "$plugin_target" ]]; then
    current="$(readlink "$plugin_target")"
    case "$current" in
      "$plugin_source"|"$HOME/agent-cockpit/agent-mail-tools/agent-mail.opencode-plugin.js")
        ln -sfn "$plugin_source" "$plugin_target" 2>/dev/null || \
          echo "警告: 无法更新 agent-mail 插件软链" >&2 ;;
      *) echo "警告: 保留用户已有 agent-mail 插件软链: $plugin_target" >&2 ;;
    esac
  elif [[ -f "$plugin_target" ]]; then
    current_hash="$(python3 - "$plugin_target" <<'PY' 2>/dev/null || true
import hashlib
import sys
from pathlib import Path
print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"
    if [[ "$current_hash" != "$legacy_plugin_sha256" ]]; then
      echo "ACTIVATION_BLOCK: 保留未知 regular agent-mail 插件: $plugin_target" >&2
    elif [[ -e "$plugin_backup" || -L "$plugin_backup" ]]; then
      echo "ACTIVATION_BLOCK: rollback 备份已存在: $plugin_backup" >&2
    elif mv "$plugin_target" "$plugin_backup"; then
      if ! ln -s "$plugin_source" "$plugin_target"; then
        mv "$plugin_backup" "$plugin_target" 2>/dev/null || true
        echo "ACTIVATION_BLOCK: 插件激活失败，已尝试回滚" >&2
      fi
    else
      echo "ACTIVATION_BLOCK: 无法创建 plugin rollback 备份" >&2
    fi
  else
    echo "ACTIVATION_BLOCK: 保留用户已有 agent-mail 插件路径: $plugin_target" >&2
  fi
fi
