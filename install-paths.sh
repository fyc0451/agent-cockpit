#!/usr/bin/env bash

# install.sh / upgrade.sh / launchd.sh 共用的安装路径编码。
# 文件系统路径不是 shell 命令；进入 systemd unit、sed 或 plist 前必须按目标格式编码。

ac_validate_install_dir() {
  local value="${1:-}"
  if [[ "$value" != /* || "$value" =~ [[:cntrl:]] ]]; then
    echo "安装路径必须是绝对路径且不能包含控制字符: $value" >&2
    return 1
  fi
}

ac_escape_sed_replacement() {
  local value="$1"
  value=${value//\\/\\\\}
  value=${value//|/\\|}
  value=${value//&/\\&}
  printf '%s' "$value"
}

ac_escape_systemd_value() {
  local value="$1"
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//%/%%}
  printf '%s' "$value"
}

ac_escape_systemd_exec_value() {
  local value
  value="$(ac_escape_systemd_value "$1")"
  # ExecStart 支持 $VAR / ${VAR} 展开；$$ 才表示字面量美元符号。
  value=${value//\$/\$\$}
  printf '%s' "$value"
}

ac_escape_plist_value() {
  local value="$1"
  # 反斜杠保护 replacement 中的 &,兼容 Bash 3.2 与较新的 patsub_replacement。
  value=${value//&/\&amp;}
  value=${value//</\&lt;}
  value=${value//>/\&gt;}
  printf '%s' "$value"
}

ac_client_env_loopback_hub() {
  # 从 client.env 严格解析 loopback hub URL，成功时输出 "HOST PORT"。
  # hub= 必须是 http://127.0.0.1[:PORT]/ 或 http://localhost[:PORT]/（PORT 缺省 8765）；
  # 远程 hub、缺 hub= 或非 loopback HTTP 一律返回非零。
  local file="${1:-}" value port port_number
  [[ -f "$file" ]] || return 1
  value="$(sed -n 's/^hub=//p' "$file" | tail -n 1)"
  [[ -n "$value" ]] || return 1
  if [[ "$value" =~ ^http://(127\.0\.0\.1|localhost)(:([0-9]{1,5}))?/?$ ]]; then
    port="${BASH_REMATCH[3]:-8765}"
    port_number=$((10#$port))
    (( port_number >= 1 && port_number <= 65535 )) || return 1
    printf '%s %s\n' "${BASH_REMATCH[1]}" "$port_number"
    return 0
  fi
  return 1
}
