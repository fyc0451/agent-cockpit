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
# Agent Mail paths are also resolved while modules are imported. Keep the
# required gate completely outside the real Hub database and user registry.
export AGENT_MAIL_DB_PATH="$pytest_temp/agent-mail/storage.sqlite3"
export AGENT_MAIL_REGISTRY_DIR="$pytest_temp/agent-mail/registry"
export AGENT_MAIL_CLIENT_ENV="$pytest_temp/agent-mail/client.env"
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
  tests/test_team_sessions.py \
  tests/test_team_session_bindings.py \
  tests/test_team_lead_worker.py \
  tests/test_team_agent_reply.py \
  tests/test_persist_work.py \
  tests/test_next_dev_profile.py \
  tests/test_workspace_write_gate.py \
  tests/test_store_schema.py \
  tests/test_release_files.py \
  tests/test_upgrade_retired.py \
  tests/test_upgrade_script.py

printf '%s\n' 'fast-required: web unit tests'
npm --prefix web test -- --run

printf '%s\n' 'fast-required: web typecheck and production build'
npm --prefix web run build

printf '%s\n' 'fast-required: passed'
