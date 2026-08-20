# Agent Cockpit

[![test](https://github.com/fyc0451/agent-cockpit/actions/workflows/test.yml/badge.svg)](https://github.com/fyc0451/agent-cockpit/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)

> A web-based control cockpit for CLI coding agents running under [herdr](https://herdr.dev).
> See every agent's status at a glance, drop into a live terminal, send prompts,
> upload screenshots, and orchestrate your fleet — from any browser, including your phone.

[中文](README.md) | [日本語](README.ja.md)

## Current product: Cockpit 3.0

Cockpit 3.0 is a group chat at `http://127.0.0.1:8790/#/chat`.
The UI is a waterfall, a member list, and a composer — not the old kanban board.
Product lines are 3.0 and a planned 4.0. There is no 2.0 / 3.5 install path.

The current entry is `install.sh`: it builds `web/dist` and starts
`scripts/dev_server.py`. The old board is no longer the install result.

## What it does

- **Group-chat waterfall** — CLI agents under herdr show up as members; conclusions land in bubbles, process folds away.
- **Queue by default** — Enter queues until the pane is idle; use Interrupt only to stop current work.
- **Harvest** — conclusions are collected on pane `idle` / `done` only.
- **Files and attachments** — browse the repo and copy paths from chat; attachments stay collapsed by default.
- **Settings** — appearance, source one-click upgrade, environment check.
- **Mobile** — same Hash-routed chat in a phone browser.

## How it works

```
Browser (Mac / phone)
    │  LAN / VPN (:8790)
    ▼
Agent Cockpit (FastAPI, same host as herdr + hub)
    ├── optionally reads Agent Mail SQLite         (read-only, WAL)
    ├── optionally writes via Agent Mail hub MCP   (send / ack)
    ├── reads  herdr sockets (all sessions)        (pane status / output)
    └── pushes state and diffs to the browser over SSE
```

It deploys **on the same machine as herdr**. Your laptop and phone are just browsers.
Agent Mail is required to create workspaces and add agents. If the hub is down,
existing chat bubbles stay readable.

## Install 3.0

Run this on the same host as herdr.

| Dependency | Why |
| --- | --- |
| [herdr](https://herdr.dev) | Terminal sessions that hold the agents |
| Git, Python 3.12+, Node.js 20+ (with npm) | Clone, run the server, build `web/dist` |
| [Agent Mail](https://github.com/Dicklesworthstone/mcp_agent_mail) hub (`:8765`) | Identity and chat delivery |
| At least one signed-in Agent CLI | Codex / Claude / Kimi / OpenCode / Grok / Qoder CLI CN |

Clone the repo anywhere. You do not need `$HOME/github`. The discovery root
defaults to the parent of the checkout, or the checkout itself when that parent
would be Home. Set `COCKPIT_PROJECT_ROOT` only if you want a different existing
directory of repositories (not Home itself).

```bash
curl -fsSL https://raw.githubusercontent.com/fyc0451/agent-cockpit/main/install.sh | bash
```

The installer clones to `~/agent-cockpit` (or installs in place), creates a
venv, installs Agent Mail, builds `web/dist`, and registers
`agent-cockpit.service` (LaunchAgent on macOS). Open
`http://127.0.0.1:8790/#/chat`.

If a Hub is already reachable it is reused and a hand-maintained
`~/.agent-mail/client.env` is not overwritten. Set `AGENT_MAIL_SKIP_HUB=1` to
skip the local Hub. If 8790 is busy, stop that process; do not change the port.
Run `./doctor.sh` if startup fails.

Do **not** run `.venv/bin/python server.py` by itself: without
`COCKPIT_NEXT_PROFILE=dev` the homepage is not 3.0. The service unit already
starts `scripts/dev_server.py`.

Manual install without systemd:

```bash
git clone https://github.com/fyc0451/agent-cockpit.git
cd agent-cockpit
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./install-agent-mail-tools.sh .
./install-agent-mail-hub.sh
npm ci --prefix web
npm run --prefix web build
.venv/bin/python scripts/dev_server.py
# → http://127.0.0.1:8790/#/chat
```

### LAN (optional)

```bash
install -d -m 700 "$HOME/.config/agent-cockpit"
(umask 077; set -o noclobber; openssl rand -hex 32 > \
  "$HOME/.config/agent-cockpit/cockpit.token")
COCKPIT_HOST=0.0.0.0 .venv/bin/python scripts/dev_server.py
```

Open `http://<LAN-IP>:8790` and paste `~/.config/agent-cockpit/cockpit.token`.
Do not put the token in `.env`, chat, or logs.

> **Security warning:** use HTTPS or Tailscale Serve for remote access. Plain HTTP
> exposes the login session cookie to anyone able to observe the local network.
> Do not expose Agent Cockpit directly to the public Internet.

### Do not use these as the 3.0 install

| Entry | What you actually get |
| --- | --- |
| `scripts/next_dev.py` / `:18790` | Frozen Next 2.0 preview, not 3.0 |
| `./upgrade.sh` | Retired (fail-closed) |
| Native V2 from GitHub Latest | Replaces source 8790 with the packaged unit |

After source 8790 is running, Settings has a one-click upgrade that pulls the
official tag, rebuilds `web/dist`, and restarts the source unit. Leave
`COCKPIT_UPGRADE_V2_ENABLED` off.

## First run

1. Confirm herdr is running with at least one signed-in agent pane.
2. Open `http://127.0.0.1:8790/#/chat`.
3. Pick a workspace / herdr session on the left; members appear on the right.
4. The composer defaults to **queue**. `@` a member and press Enter; they run
   when idle. Use **interrupt** only to stop current work.
5. Replies land in the waterfall. Long process text folds under 「展开过程」.
6. Settings covers appearance, doctor, and source upgrade.

Hash routes: chat `/#/chat`, settings `/#/settings`. Unknown paths fall back
to chat. The old board remains as `static/index.html` in the tree; the install
entry no longer starts it. See [docs/USER-GUIDE.md](docs/USER-GUIDE.md).

## Usage (old board, install.sh only)

### Board

- Four live columns: **⚠ Needs you / ⚡ Working / ✓ Done / ○ Idle**.
- Click a card to open the pane's Flow view; click **🖥** on a card to take over its TUI.
- The **🚀 Launch bar** at the bottom: pick an existing session + agent type + workdir,
  then **+ New agent**. With no session at all, it guides you to create a workspace.

### Inbox

- One queue for agents waiting on input, failed background tasks, pending diffs, and
  Agent Mail unread.
- Click an item to jump to where it's handled; click **Enable notifications** for Web Push.
- Push requires a secure context: `https://` (e.g. Tailscale Serve) or
  `http://localhost`. On iPhone/iPad, first **Share → Add to Home Screen** in Safari,
  then launch from the Home Screen icon and enable notifications there (iOS does not
  allow Web Push permission from a normal Safari tab).

### Terminal

- **+ New terminal** opens a browser PTY (it dies with the server — no persistent jobs).
- Toolbar: 📎 upload (images/files auto-insert as `@/path`), @Collab (insert who the
  agent can talk to), 🖥 herdr (attach to a herdr session with split panes),
  📜 Flow view, 📋 Copy to clipboard.
- herdr keys: `Ctrl-b` switch pane / `d` detach / `?` all shortcuts.
- On phones, tap **⌨ Keyboard** for arrow/Ctrl keys and a visible input box.

### Flow (herdrflow)

- One panel per agent pane: scrollable, copyable output plus a quick-prompt input.
- `prompt` mode uses the agent's prompt interface; `send` mode types the keys directly.
- 📋 fills the input with what you just copied in the Herdr TUI; ⛶ focuses one
  workspace fullscreen.

### Sessions

- Lists all herdr sessions: clean-restart / resume-restart panes, stop, delete stopped
  sessions.
- **+ Quick workspace**: automatically creates the session → splits panes → starts
  agents; with Agent Mail installed it also registers identities and notifies them,
  and **📧 Init mail** registers a full set of agent-mail identities in one click.
- Each session persists one Agent Mail project. Linked worktrees from the same clone
  resolve to the main worktree; ambiguous legacy sessions ask you to choose instead
  of guessing from the first pane cwd.

### Messages

- Browse Agent Mail by project/agent, send messages, ack unread.
- When Agent Mail is missing or the hub is offline, the view degrades gracefully:
  existing messages stay read-only; everything else is unaffected.

### Files

- The "Accessible locations" at the top are whitelisted roots: system directories +
  registered projects + custom directories. **＋ Add directory** whitelists any
  directory (browse permission only — nothing is moved).
- Click directories to descend, files to view; text files can be edited and saved in
  place, others download with ⬇️.
- Search matches filenames recursively under the current directory.
- **🚀 New workspace here** pre-fills the quick-workspace dialog with the current
  directory.

### Settings

- **Language**: 中文 / English / 日本語; **Appearance**: dark / light (in light mode,
  explicit dark backgrounds painted by TUIs are luminance-inverted, so even opencode's
  black canvas stays readable).
- **Terminal font size**: 10–24, per-device, effective immediately.
- **Default agent per directory**: the launch bar preselects it when you type a workdir.
- **Enabled agents**: launch menus only list the checked types.
- **Runtime limits**: upload limit, max terminals, idle terminal TTL, terminal write timeout.
- **Environment check**: readiness of herdr / each agent executable / Agent Mail —
  ❌ means not installed.

## Configuration

Configuration is read from environment variables (see `.env.example`):

| Var | Default | Description |
| --- | --- | --- |
| `COCKPIT_HOST` | `127.0.0.1` | Bind address |
| `COCKPIT_PORT` | `8790` | Port |
| `COCKPIT_TOKEN` | empty | Shared login token; required for non-loopback binds |
| `HERDR_BIN` | auto-detected | Path to herdr binary |
| `CODEX_BIN` | auto-detected | Path to codex binary |
| `AGENT_MAIL_DB_PATH` | auto-detected | Custom Agent Mail `storage.sqlite3` path |
| `COCKPIT_VAPID_SUBJECT` | `mailto:agent-cockpit@localhost` | Web Push VAPID contact claim |
| `COCKPIT_VAPID_PRIVATE_KEY` / `PUBLIC_KEY` | auto-generated | Optional fixed VAPID key pair for multi-instance deployments |

The Agent Mail database is detected under the new `~/.local/share/mcp_agent_mail/`
location and the legacy `~/mcp_agent_mail/` location. The hub token is read from
`~/.agent-mail/client.env` automatically — never hardcode it.
VAPID keys are generated once under `~/dashboard-data/`; they never enter the repository.
User settings live in `~/dashboard-data/settings.json`; per-device preferences like
terminal font size live in the browser's localStorage. Session-to-mail-project bindings
live in `~/dashboard-data/mail-projects.json` and contain no identity tokens.

## Upgrade, diagnostics, and uninstall

```bash
./upgrade.sh       # RETIRED (fail-closed): one-click updater disabled; managed release only
./doctor.sh        # checks Python, dependencies, Herdr, Agent Mail, auth, and service
./uninstall.sh     # removes only the user service; code, config, and data are preserved
```

Run the test suite with `.venv/bin/pip install -r requirements-dev.txt` followed by
`.venv/bin/pytest -q`.

## Project structure

```
agent-cockpit/
├── scripts/dev_server.py  3.0 source-8790 launcher (current install)
├── server.py              Compatibility entry (old board without NEXT_PROFILE)
├── source_native_migrate.py / release_lane.py  Managed release entry points
├── agent_cockpit/         Application implementation (server, chat ledger, mail, upgrade)
├── web/                   3.0 group-chat frontend (build output: web/dist)
├── agent_mail_commands/    Agent Mail command implementation
├── static/index.html      Old board leftover (install no longer starts it)
├── tests/                 Regression and security tests
├── install.sh             3.0 one-command install (web/dist + dev_server)
├── upgrade.sh             Retired
├── doctor.sh / uninstall.sh
├── agent-cockpit.service  3.0 systemd unit (ExecStart=dev_server.py)
└── launchd.sh / agent-cockpit.plist  macOS LaunchAgent
```

## Why a cockpit and not a CLI?

CLI agents (codex, kimi, qoder) are powerful but blind to each other. herdr puts them
in panes you can watch — but only from a terminal on that machine. Agent Cockpit turns
that local terminal view into a **web cockpit** you can open from your phone on the
couch, see which agent is blocked waiting for you, drop a screenshot of a bug, and let
the right agent pick it up.

## Limitations

- **GUI agents (e.g. ZCode Desktop) can't join the board** — this cockpit drives
  *terminal* CLI agents under herdr. GUI apps have no programmatic control surface.
- **Shared-token auth** — suitable for a trusted personal LAN/VPN. It is not a
  multi-user authorization system; keep the service behind a firewall or private
  overlay network.
- **Transport security** — HTTP does not protect the session cookie. Use HTTPS or
  Tailscale Serve outside a fully trusted personal network.
- **Optional Agent Mail integration** — when installed, Cockpit reads its SQLite
  directly but never writes it; all writes go through the hub MCP API. Missing or
  unavailable Agent Mail automatically degrades only message-related features.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for development instructions and
[SECURITY.md](SECURITY.md) for private vulnerability reporting and the deployment
threat model. Community participation follows [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE)
