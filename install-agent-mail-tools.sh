#!/usr/bin/env bash
set -u

INSTALL_DIR="${1:-$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"

for name in am-register am-init-project mail-send mail-recv mail-identity-inject task-report; do
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
        echo "已迁移旧 Hook 入口: $legacy_hook（备份: $backup）"
      fi
    fi
  else
    echo "警告: 保留用户已有 Hook 文件，不覆盖: $legacy_hook" >&2
  fi
fi
