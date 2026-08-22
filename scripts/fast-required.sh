#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if [[ -x .venv/bin/pytest ]]; then
  pytest_command=(.venv/bin/pytest)
elif command -v pytest >/dev/null 2>&1; then
  pytest_command=(pytest)
else
  printf '%s\n' 'fast-required: pytest not found; install requirements-dev.txt first' >&2
  exit 127
fi

pytest_temp="$(mktemp -d "${TMPDIR:-/tmp}/agent-cockpit-fast-required.XXXXXX")"
cleanup() {
  rm -rf -- "$pytest_temp"
}
trap cleanup EXIT

printf '%s\n' 'fast-required: backend contract tests'
umask 077
# Test modules import the server before pytest fixtures run. Ignore any live
# source-profile inherited from the developer shell so collection matches CI.
unset COCKPIT_NEXT_PROFILE
"${pytest_command[@]}" -q --basetemp="$pytest_temp" \
  tests/test_auth_session.py \
  tests/test_security.py \
  tests/test_exception_handlers.py \
  tests/test_files_security.py \
  tests/test_chat_ledger.py \
  tests/test_chat_ledger_api.py \
  tests/test_chat_ledger_sse.py \
  tests/test_agent_mail_tools.py \
  tests/test_hub_client.py \
  tests/test_team_ledger.py \
  tests/test_team_inbox_router.py \
  tests/test_persist_work.py \
  tests/test_workspace_write_gate.py

printf '%s\n' 'fast-required: web unit tests'
npm --prefix web test -- --run

printf '%s\n' 'fast-required: web typecheck and production build'
npm --prefix web run build

printf '%s\n' 'fast-required: passed'
