# Workspace Terminal Ticket v1

## Boundary

`terminal_ticket_store.py` persists a workspace-scoped declaration used by a later
terminal controller. It does not create a PTY, execute a command, retain a path, PID,
environment, terminal output, terminal input, or runtime-provider handle. It does not
modify `terminal.py`, shared server wiring, runtime paths, Activity, Event, Workspace,
Operation, or Runtime authority.

## Record and Ownership

Each opaque `ttk_` ticket is owned by exactly one opaque `prj_` project and `ws_`
workspace. A record contains `desired_state`, `observed_state` (each one of `stopped`,
`running`, `paused`, `recovery_required`, `unknown`), positive `engine_generation`, a
finite non-negative `reconnect_cursor`, typed `{type,id}` receipt references, `revision`,
and Store-assigned timestamps. Receipt references are opaque identifiers only.

`create(value, idempotency_key)` uses `(project_id, workspace_id, idempotency_key)`.
Exact canonical replay returns the original immutable record; a changed request returns
`idempotency_conflict`. `update(... expected_revision, value)` is compare-and-swap and
increments revision exactly once; `engine_generation` cannot regress; a missing, stale,
or generation-regressing row returns `revision_conflict`.
The Store never treats an ID outside its supplied project/workspace as visible.

## Persistence and Reads

Initialization is explicit and creates a private `0600` SQLite leaf. Every open validates
the v1 SQLite-object fingerprint. Missing reads do not create a file; schema drift fails
closed. Read connections use `mode=ro`, `query_only`, and an explicit snapshot. They never
initialize, migrate, enable WAL, or write. `list` is scoped to one project/workspace,
orders by opaque ticket ID, has a `1..100` limit, and returns a next cursor only when more
matching records exist.

All Store errors expose a stable `TerminalTicketError` code only and suppress raw SQLite,
path, record, and materialization details. Connections always close.

## Injectable Read API

`terminal_ticket_api.install(app, TerminalTicketApiService(store_provider))` adds only
`GET /api/projects/{project_id}/workspaces/{workspace_id}/terminal-tickets/{ticket_id}`
to a supplied FastAPI app. Its success envelope is `{data,meta}` and its error envelope is
`{error:{code,message,retryable,request_id,details}}`. This car does not wire the shared
server or enable write/control APIs.
