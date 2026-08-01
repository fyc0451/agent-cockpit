# Agent Cockpit

> A web-based control cockpit for CLI coding agents running under [herdr](https://herdr.dev).
> See every agent's status at a glance, drop into a live terminal, send prompts, upload screenshots, and orchestrate your fleet — from any browser, including your phone.

Inspired by [Orca](https://onorca.dev)'s Agent Dashboard, but built as a lightweight single-binary web app that plugs into your existing herdr sessions and an [agent-mail](https://github.com/) hub.

## What it does

- **Kanban board** — every CLI agent (codex / kimi / qoder / ...) across all herdr sessions, sorted into *Needs You / Working / Done / Idle* columns, updating in real time.
- **Live terminal** — click any agent card to open its terminal output; send prompts, run shell commands, or send special keys.
- **Screenshot → agent** — upload an image, it auto-inserts as `@/path` so codex picks it up (`Viewed Image`).
- **Agent messaging** — built on agent-mail: send/read messages between agents, ack unread.
- **File browser + editor** — browse and edit project files in a sandboxed whitelist.
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
| [agent-mail](https://github.com/) hub (`:8765`) | Provides the SQLite DB (read) + MCP write API |
| `codex` CLI (authenticated) | For background `codex exec` tasks |
| Python 3.12+ | Runtime |

## Quick start

```bash
# 1. Clone
git clone https://github.com/YOUR/agent-cockpit.git
cd agent-cockpit

# 2. Use the hub's venv (zero new deps — it already has fastapi/uvicorn/etc.)
#    Or create your own: python -m venv .venv && .venv/bin/pip install fastapi uvicorn ...
export PYTHONPATH=.

# 3. Run
python server.py
# → http://localhost:8790
```

## Deploy as a systemd service

```bash
# Copy the unit file
cp agent-mail-dashboard.service ~/.config/systemd/user/agent-cockpit.service
# Edit paths to match your setup, then:
systemctl --user daemon-reload
systemctl --user enable --now agent-cockpit
systemctl --user status agent-cockpit
```

See `agent-mail-dashboard.service` for the unit template.

## Configuration

All optional, via environment variables (see `.env.example`):

| Var | Default | Description |
| --- | --- | --- |
| `COCKPIT_HOST` | `0.0.0.0` | Bind address |
| `COCKPIT_PORT` | `8790` | Port |
| `HERDR_BIN` | auto-detected | Path to herdr binary |
| `CODEX_BIN` | auto-detected | Path to codex binary |

The hub token is read from `~/.agent-mail/client.env` automatically — never hardcode it.

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
└── agent-mail-dashboard.service   systemd unit template
```

## Why a cockpit and not a CLI?

CLI agents (codex, kimi, qoder) are powerful but blind to each other. herdr puts them in panes you can watch — but only from a terminal on that machine. Agent Cockpit turns that local terminal view into a **web cockpit** you can open from your phone on the couch, see which agent is blocked waiting for you, drop a screenshot of a bug, and let the right agent pick it up.

## Limitations

- **GUI agents (e.g. ZCode Desktop) can't join the board** — this cockpit drives *terminal* CLI agents under herdr. GUI apps have no programmatic control surface.
- **No auth** — designed for trusted LAN/VPN access only. Bind to `127.0.0.1` if you need isolation.
- **Reads the hub's SQLite directly** — never writes to it; all writes go through the hub's MCP API to preserve single-writer semantics.

## License

[MIT](LICENSE)
