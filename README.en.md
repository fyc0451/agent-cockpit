# Agent Cockpit

[![test](https://github.com/fyc0451/agent-cockpit/actions/workflows/test.yml/badge.svg)](https://github.com/fyc0451/agent-cockpit/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)

> A web-based control cockpit for CLI coding agents running under [herdr](https://herdr.dev).
> See every agent's status at a glance, drop into a live terminal, send prompts,
> upload screenshots, and orchestrate your fleet — from any browser, including your phone.

[中文](README.md) | [日本語](README.ja.md)

<p align="center">
  <img src="docs/screenshots/board-desktop.png" alt="Board (desktop)" width="74%">
  <img src="docs/screenshots/board-mobile.png" alt="Board (phone)" width="22%">
</p>

Inspired by [Orca](https://onorca.dev)'s Agent Dashboard, but built as a lightweight
web app that plugs into your existing Herdr sessions.
[Agent Mail](https://github.com/Dicklesworthstone/mcp_agent_mail) integration is optional.

## What it does

- **Kanban board** — every CLI agent (codex / kimi / claude / qoder / grok / opencode) across all herdr sessions, sorted into *Needs You / Working / Done / Idle* columns, updating in real time.
- **Live terminal** — click any agent card to open its terminal output; send prompts, run shell commands, or send special keys via xterm.js.
- **Screenshot → agent** — upload an image, it auto-inserts as `@/path` so the agent picks it up (`Viewed Image`).
- **Attention Inbox** — blocked agents, failed background tasks, pending diffs, and optional Agent Mail unread in one actionable queue.
- **Web Push** — opt in from the Inbox and jump from a notification straight to the pane, task, or message that needs you.
- **Agent messaging** — built on Agent Mail: send/read messages between agents, ack unread. Without Agent Mail the message views hide themselves and everything else keeps working.
- **File browser + editor** — browse, edit, download, and upload project files inside a sandboxed whitelist.
- **codex tasks** — kick off background `codex exec` jobs, watch output stream, review diffs, apply/stash changes.
- **Mobile-friendly** — responsive single-file frontend, camera upload, touch-friendly, installable as a PWA.
- **Dark / light theme** — toggle in the header, remembered across sessions. In light mode, explicit dark colors painted by TUIs (e.g. opencode's black background) are automatically inverted to a readable palette.

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

It deploys **on the same machine as herdr**, reading everything locally for zero-latency
access. Your Mac and phone are just browser clients. If Agent Mail is absent, only the
message views are hidden; if its Hub is temporarily down, existing messages remain
readable while send/ack becomes read-only. The board, terminals, files, tasks, Inbox,
and push notifications continue to work in either case.

## Install

### Prerequisites

| Dependency | Why |
| --- | --- |
| [herdr](https://herdr.dev) | The agent sessions this cockpit visualizes and controls |
| [Agent Mail](https://github.com/Dicklesworthstone/mcp_agent_mail) hub (`:8765`) (optional) | Adds cross-agent messages to the Inbox and message view |
| `codex` CLI (authenticated) | For background `codex exec` tasks |
| Python 3.12+ | Runtime |

### One-command install

```bash
curl -fsSL https://raw.githubusercontent.com/fyc0451/agent-cockpit/main/install.sh | bash
```

The installer clones to `~/agent-cockpit`, creates a virtual environment, installs
dependencies, and registers `agent-cockpit.service` on Linux or a LaunchAgent on
macOS. Bundled Agent Mail helpers are safely linked into `~/.local/bin`; existing
regular files and custom symlinks are preserved. Run `~/agent-cockpit/doctor.sh` if
startup fails.

### Manual install

```bash
git clone https://github.com/fyc0451/agent-cockpit.git
cd agent-cockpit
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python server.py
# → http://localhost:8790
```

To access the cockpit from another LAN/VPN device, copy `.env.example` to `.env`,
set `COCKPIT_HOST=0.0.0.0`, and set `COCKPIT_TOKEN` to a random value. The server
refuses a non-loopback bind without a token.

> **Security warning:** use HTTPS or Tailscale Serve for remote access. Plain HTTP
> exposes the login session cookie to anyone able to observe the local network.
> Do not expose Agent Cockpit directly to the public Internet.

### Deploy as a systemd service

```bash
loginctl enable-linger "$USER"   # keep the user service running after logout
cp agent-cockpit.service ~/.config/systemd/user/agent-cockpit.service
# Edit paths to match your setup, then:
systemctl --user daemon-reload
systemctl --user enable --now agent-cockpit
```

`KillMode=process` preserves independent Herdr sessions when the cockpit is restarted;
browser-created PTYs may still disconnect and should not be treated as persistent jobs.

For a manual launch, load `.env` first:

```bash
set -a; source .env; set +a
.venv/bin/python server.py
```

## First run (5-minute quickstart)

1. Open `http://localhost:8790` in a browser.
2. An empty board is normal — click **🚀 Create your first workspace** in the empty
   state (or **+ Quick workspace** on the Sessions page), fill in a session name, the
   project directory, and the agents to start (e.g. `codex,kimi`), then launch. The
   session is created automatically if it doesn't exist.
3. Back on the **Board**, agents sort themselves into columns by status; click a card
   to see its output, click the 🖥 on a card to take over its TUI.
4. The **Inbox** collects blocked/failed agents in one place — enable browser
   notifications there.
5. If anything misbehaves, check **Settings → Environment check**: herdr, each agent's
   executable, and Agent Mail readiness at a glance. On the command line, `./doctor.sh`
   does the same.

## Usage

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
├── server.py              Compatibility server entry point
├── source_native_migrate.py / release_lane.py  Managed release entry points
├── agent_cockpit/         Application implementation (server, terminal, mail, upgrade)
├── agent_mail_commands/    Agent Mail command implementation
├── static/index.html      Single-file frontend (kanban + Inbox + terminal + tabs)
├── static/sw.js           Web Push service worker and deep-link handler
├── static/manifest.webmanifest  Root-scope installable Web App metadata
├── tests/                 Regression and security tests
├── install.sh / upgrade.sh (retired) / doctor.sh / uninstall.sh
├── agent-cockpit.service  systemd user unit template
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
