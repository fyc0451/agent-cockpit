#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=install-paths.sh
source "$INSTALL_DIR/install-paths.sh"
ac_validate_install_dir "$INSTALL_DIR"

# launchd 和普通非登录 SSH 不一定继承 Homebrew PATH。正常 Linux 路径不变。
if [[ "$(uname -s)" == "Darwin" ]]; then
  export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"
fi

fail() {
  printf 'upgrade_failed: %s\n' "$1" >&2
  exit 1
}

if ! git -C "$INSTALL_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  fail "不是 git 安装目录: $INSTALL_DIR"
fi
if ! git -C "$INSTALL_DIR" diff --quiet --ignore-submodules -- \
  || ! git -C "$INSTALL_DIR" diff --cached --quiet --ignore-submodules --; then
  fail "工作区有未提交的 tracked 修改；请先提交或还原后再升级"
fi

branch="$(git -C "$INSTALL_DIR" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
[[ -n "$branch" ]] || fail "当前为 detached HEAD，无法跟踪上游升级"
upstream="$(git -C "$INSTALL_DIR" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
[[ -n "$upstream" ]] || fail "当前分支没有上游，无法自动升级"
remote="$(git -C "$INSTALL_DIR" config --get "branch.$branch.remote" || true)"
[[ -n "$remote" && "$remote" != "." ]] || fail "当前分支没有远程上游，无法自动升级"

state_root="${XDG_STATE_HOME:-$HOME/.local/state}/agent-cockpit"
[[ ! -L "$state_root" ]] || fail "升级状态目录不得为符号链接: $state_root"
mkdir -p -- "$state_root"
[[ -d "$state_root" && ! -L "$state_root" ]] \
  || fail "升级状态目录不可用: $state_root"
chmod 700 "$state_root" 2>/dev/null || fail "无法保护升级状态目录"
lock_dir="$state_root/source-upgrade.lock.d"
if ! mkdir "$lock_dir" 2>/dev/null; then
  [[ -d "$lock_dir" && ! -L "$lock_dir" ]] \
    || fail "升级锁路径不是安全目录"
  [[ ! -L "$lock_dir/owner" ]] || fail "升级锁 owner 不得为符号链接"
  unknown_entry="$(find "$lock_dir" -mindepth 1 -maxdepth 1 ! -name owner -print -quit 2>/dev/null || true)"
  [[ -z "$unknown_entry" ]] || fail "升级锁目录包含未知内容，拒绝清理"
  lock_pid="$(sed -n 's/^pid=//p' "$lock_dir/owner" 2>/dev/null | head -n 1 || true)"
  if [[ "$lock_pid" =~ ^[0-9]+$ ]] && kill -0 "$lock_pid" 2>/dev/null; then
    fail "已有升级任务进行中(pid=$lock_pid)"
  fi
  rm -f -- "$lock_dir/owner"
  rmdir -- "$lock_dir" 2>/dev/null || fail "升级锁目录包含未知内容，拒绝清理"
  mkdir "$lock_dir" 2>/dev/null || fail "无法获取升级锁"
fi
printf 'pid=%s\n' "$$" > "$lock_dir/owner"
cleanup_lock() {
  local owner_pid
  owner_pid="$(sed -n 's/^pid=//p' "$lock_dir/owner" 2>/dev/null | head -n 1 || true)"
  if [[ "$owner_pid" == "$$" && ! -L "$lock_dir/owner" ]]; then
    rm -f -- "$lock_dir/owner"
    rmdir -- "$lock_dir" 2>/dev/null || true
  fi
}
trap cleanup_lock EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
chmod 600 "$lock_dir/owner" 2>/dev/null || fail "无法保护升级锁 owner"

health_url() {
  local port=8790 configured
  if [[ -f "$INSTALL_DIR/.env" ]]; then
    configured="$(sed -nE 's/^[[:space:]]*COCKPIT_PORT[[:space:]]*=[[:space:]]*"?([0-9]+)"?[[:space:]]*$/\1/p' "$INSTALL_DIR/.env" | tail -n 1)"
    [[ -z "$configured" ]] || port="$configured"
  fi
  printf 'http://127.0.0.1:%s/health/live\n' "$port"
}

wait_for_health() {
  local python_bin="$INSTALL_DIR/.venv/bin/python"
  [[ -x "$python_bin" ]] || return 1
  "$python_bin" - "$(health_url)" <<'PY'
import json
import sys
import time
import urllib.request

url = sys.argv[1]
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            payload = json.load(response)
        if response.status == 200 and payload.get("status") == "live":
            raise SystemExit(0)
    except Exception:
        pass
    time.sleep(1)
raise SystemExit(1)
PY
}

from_sha="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
printf '正在检查上游更新: %s -> %s\n' "$branch" "$upstream"
git -C "$INSTALL_DIR" fetch --prune "$remote"
target_sha="$(git -C "$INSTALL_DIR" rev-parse "${upstream}^{commit}")"

if [[ "$from_sha" == "$target_sha" ]]; then
  printf '已是最新版本: %.12s\n' "$from_sha"
  exit 0
fi
if ! git -C "$INSTALL_DIR" merge-base --is-ancestor "$from_sha" "$target_sha"; then
  fail "本地分支领先或已与上游分叉；拒绝覆盖本地提交"
fi

rollback() {
  local primary="$1" reset_rc install_rc health_rc
  printf '升级失败(%s)，正在回滚到 %.12s...\n' "$primary" "$from_sha" >&2
  set +e
  git -C "$INSTALL_DIR" reset --hard "$from_sha" >/dev/null
  reset_rc=$?
  if [[ "$reset_rc" -eq 0 ]]; then
    bash "$INSTALL_DIR/install.sh"
    install_rc=$?
  else
    install_rc=1
  fi
  if [[ "$install_rc" -eq 0 ]]; then
    wait_for_health
    health_rc=$?
  else
    health_rc=1
  fi
  set -e
  if [[ "$reset_rc" -eq 0 && "$install_rc" -eq 0 && "$health_rc" -eq 0 ]]; then
    printf '已回滚并恢复旧版本 %.12s。\n' "$from_sha" >&2
  else
    printf 'rollback_failed: reset=%s install=%s health=%s；请人工恢复服务。\n' \
      "$reset_rc" "$install_rc" "$health_rc" >&2
  fi
  exit 1
}

printf '正在快进到 %.12s...\n' "$target_sha"
git -C "$INSTALL_DIR" merge --ff-only "$target_sha" >/dev/null \
  || rollback "fast_forward_failed"

if ! bash "$INSTALL_DIR/install.sh"; then
  rollback "install_failed"
fi
if ! wait_for_health; then
  rollback "health_failed"
fi

printf '升级完成: %.12s -> %.12s\n' "$from_sha" "$target_sha"
printf 'Agent Cockpit 已通过 /health/live 检查；Herdr session 和 pane 未重启。\n'
