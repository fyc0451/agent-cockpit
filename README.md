# Agent Cockpit

> A web-based control cockpit for CLI coding agents running under [herdr](https://herdr.dev).
> See every agent's status at a glance, drop into a live terminal, send prompts, upload screenshots, and orchestrate your fleet — from any browser, including your phone.

Inspired by [Orca](https://onorca.dev)'s Agent Dashboard, but built as a lightweight web app that plugs into your existing Herdr sessions and an [Agent Mail](https://github.com/Dicklesworthstone/mcp_agent_mail) hub.

## What it does

- **Kanban board** — every CLI agent (codex / kimi / qoder / ...) across all herdr sessions, sorted into *Needs You / Working / Done / Idle* columns, updating in real time.
- **Live terminal** — click any agent card to open its terminal output; send prompts, run shell commands, or send special keys.
- **Screenshot → agent** — upload an image, it auto-inserts as `@/path` so codex picks it up (`Viewed Image`).
- **Agent messaging** — built on agent-mail: send/read messages between agents, ack unread.
- **File browser + editor** — browse, edit, and download project files in a sandboxed whitelist.
- **codex tasks** — kick off background `codex exec` jobs, watch output stream, review diffs, apply/stash changes.
- **Mobile-friendly** — responsive single-file frontend, camera upload, touch-friendly.
- **Dark / light theme** — toggle in the header, remembered across sessions.

## How it works

```
Browser (Mac / phone)
    │  LAN / VPN (:8790)
    ▼
Agent Cockpit (FastAPI, same host as herdr + hub)
    ├── reads  ~/mcp_agent_mail/storage.sqlite3  (read-only, WAL)
    ├── writes via hub MCP at 127.0.0.1:8765      (send / ack)
    ├── reads  herdr sockets (all sessions)       (pane status / output)
    └── pushes diffs to the browser over SSE
```

It deploys **on the same machine as herdr + the agent-mail hub**, reading everything locally for zero-latency access. Your Mac and phone are just browser clients.

## Prerequisites

| Dependency | Why |
| --- | --- |
| [herdr](https://herdr.dev) | The agent sessions this cockpit visualizes and controls |
| [Agent Mail](https://github.com/Dicklesworthstone/mcp_agent_mail) hub (`:8765`) | Provides the SQLite DB (read) + MCP write API |
| `codex` CLI (authenticated) | For background `codex exec` tasks |
| Python 3.12+ | Runtime |

## One-command install

```bash
curl -fsSL https://raw.githubusercontent.com/fyc0451/agent-cockpit/main/install.sh | bash
```

The installer clones to `~/agent-cockpit`, creates a virtual environment, installs
dependencies, and enables `agent-cockpit.service` when a systemd user bus is
available. Run `~/agent-cockpit/doctor.sh` if startup fails.

## Manual install

```bash
# 1. Clone
git clone https://github.com/fyc0451/agent-cockpit.git
cd agent-cockpit

# 2. Install the pinned dependencies
python -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Run locally (authentication is optional on loopback)
.venv/bin/python server.py
# → http://localhost:8790
```

To access the cockpit from another LAN/VPN device, copy `.env.example` to `.env`,
set `COCKPIT_HOST=0.0.0.0`, and set `COCKPIT_TOKEN` to a random value. The server
refuses a non-loopback bind without a token.

> **Security warning:** use HTTPS or Tailscale Serve for remote access. Plain HTTP
> exposes the login session cookie to anyone able to observe the local network.
> Do not expose Agent Cockpit directly to the public Internet.

The systemd unit loads `.env` automatically. For a manual launch, load it first:

```bash
set -a
source .env
set +a
.venv/bin/python server.py
```

Set `COCKPIT_TOKEN` when a reverse proxy or Tailscale Serve exposes the service too,
even if the Python process itself only listens on `127.0.0.1`.

## Deploy as a systemd service

```bash
# Keep the user service running after logout (may require administrator policy)
loginctl enable-linger "$USER"

# Copy the unit file
cp agent-cockpit.service ~/.config/systemd/user/agent-cockpit.service
# Edit paths to match your setup, then:
systemctl --user daemon-reload
systemctl --user enable --now agent-cockpit
systemctl --user status agent-cockpit
```

See `agent-cockpit.service` for the unit template. `KillMode=process` preserves
independent Herdr sessions when the cockpit is restarted; browser-created PTYs may
still disconnect and should not be treated as persistent jobs.

## Upgrade, diagnostics, and uninstall

```bash
./upgrade.sh       # refuses to overwrite local tracked changes
./doctor.sh        # checks Python, dependencies, Herdr, Agent Mail, auth, and service
./uninstall.sh     # removes only the user service; code, config, and data are preserved
```

## Configuration

Configuration is read from environment variables (see `.env.example`):

| Var | Default | Description |
| --- | --- | --- |
| `COCKPIT_HOST` | `127.0.0.1` | Bind address |
| `COCKPIT_PORT` | `8790` | Port |
| `COCKPIT_TOKEN` | empty | Shared login token; required for non-loopback binds |
| `HERDR_BIN` | auto-detected | Path to herdr binary |
| `CODEX_BIN` | auto-detected | Path to codex binary |

The hub token is read from `~/.agent-mail/client.env` automatically — never hardcode it.

Run the test suite with `.venv/bin/pip install -r requirements-dev.txt` followed by
`.venv/bin/pytest -q`.

## Project structure

```
agent-cockpit/
├── server.py              FastAPI app: routes, SSE, static serving
├── db.py                  Read-only queries against the hub's SQLite
├── herdr_client.py        Multi-session herdr CLI wrapper (board data source)
├── tasks.py               codex exec task runner + diff/apply
├── files.py               Sandboxed file browser/editor
├── hub_client.py          MCP write proxy (send_message / ack)
├── uploads.py             File/screenshot upload sink
├── static/index.html      Single-file frontend (kanban + terminal + tabs)
├── tests/                 Regression and security tests
├── install.sh             One-command installer
├── upgrade.sh             Safe fast-forward updater
├── doctor.sh              Environment diagnostics
└── agent-cockpit.service  systemd user unit template
```

## Why a cockpit and not a CLI?

CLI agents (codex, kimi, qoder) are powerful but blind to each other. herdr puts them in panes you can watch — but only from a terminal on that machine. Agent Cockpit turns that local terminal view into a **web cockpit** you can open from your phone on the couch, see which agent is blocked waiting for you, drop a screenshot of a bug, and let the right agent pick it up.

## Limitations

- **GUI agents (e.g. ZCode Desktop) can't join the board** — this cockpit drives *terminal* CLI agents under herdr. GUI apps have no programmatic control surface.
- **Shared-token auth** — suitable for a trusted personal LAN/VPN. It is not a multi-user authorization system; keep the service behind a firewall or private overlay network.
- **Transport security** — HTTP does not protect the session cookie. Use HTTPS or Tailscale Serve outside a fully trusted personal network.
- **Reads the hub's SQLite directly** — never writes to it; all writes go through the hub's MCP API to preserve single-writer semantics.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for development instructions and
[SECURITY.md](SECURITY.md) for private vulnerability reporting and the deployment
threat model. Community participation follows [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE)
