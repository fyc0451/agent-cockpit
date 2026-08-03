#!/usr/bin/env bash
set -u

INSTALL_DIR="${1:-$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"

for name in am-register am-init-project mail-send mail-recv; do
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
      "$HOME/agent-mail-tools/$name"|"$INSTALL_DIR/agent-mail-tools/$name")
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
