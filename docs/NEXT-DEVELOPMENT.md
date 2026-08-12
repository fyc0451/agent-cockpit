# Cockpit Next development runtime

Cockpit Next is developed from `/home/fyc/github/agent-cockpit-next` while the
deployed `0.3.4` service remains frozen on `127.0.0.1:8790`.

## N0 boundary

- Branch: `next`, based on `169d0af7751b568e813d2cbca285a9f147e86001`.
- Project key and Agent Mail project: `agent-cockpit-next` and the exact Next
  worktree path.
- Reserved Herdr session: `github-agent-cockpit-next`.
- Web endpoint: `127.0.0.1:18790`.
- Runtime roots: `~/.local/share/agent-cockpit-next-data`,
  `~/.config/agent-cockpit-next`, `~/.local/state/agent-cockpit-next`, and
  `~/.local/share/agent-cockpit-next-uploads`.
- Development uses a source process. It does not install a systemd unit. A
  future isolated unit may only be named `agent-cockpit-next.service`.
- Upgrade V2, B0, and Herdr socket state are disabled in the N0 profile.
- Source `/health/live` reports `source_sha=unknown`; the Gate separately proves
  that the `next` branch contains the exact `169d0af...` baseline.

The Gate rejects missing or extra environment keys, port `8790`, the production
unit name, production runtime roots, the old Agent Mail scope, the old Herdr
session, symlinked runtime roots, and a worktree or Git baseline mismatch.
It also removes inherited Cockpit, Herdr, XDG, Hub, Python import, virtualenv,
and dynamic linker variables before starting the source interpreter. XDG roots,
the local Hub endpoints, the Herdr config/session root, and the shared read-only
Agent Mail database path are then set explicitly; server-side queries and writes
remain restricted to the exact Next project. The fixed SHA in the Gate
intentionally cross-checks the independently versioned delivery baseline.

## Commands

Create an independent virtual environment and validate the profile:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.next.example .env.next
.venv/bin/python scripts/next_dev.py check --env-file .env.next
.venv/bin/python -m pytest -q tests/test_next_isolation.py
```

Start the source development service only after the checks pass:

```bash
.venv/bin/python scripts/next_dev.py start --env-file .env.next
```

This `start` command is the only supported entry point for the Next backend. It
hands a per-profile process lock to the server across `execve`; direct Next
server invocation without that validated launcher handoff fails closed. The N0
profile continues to reject symlinked runtime roots. Internally, canonical
identity resolution maps aliases of the same roots to one identity, while
profiles with different real roots may run concurrently. Lock metadata is only
diagnostic and is never used to signal or terminate a process. Server handoff
requires both a conflicting independent lock probe and successful nonblocking
lock reassertion on the inherited descriptor; its lifespan refuses to start
without the adopted owner.

The service is expected at `http://127.0.0.1:18790`. Do not run `install.sh`,
`upgrade.sh`, `launchd.sh`, or any command targeting `agent-cockpit.service`
from this worktree.
