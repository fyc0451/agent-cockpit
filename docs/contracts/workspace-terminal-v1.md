# Workspace Terminal v1

## Boundary

This controller is the local, live implementation of a Workspace terminal ticket. It
uses the Registry-authorized RepoLocation only for active `shared` Workspaces whose
Project, Workspace and location are active, local, and available. The browser never
sends or receives a path, cwd, command, argv, PID, FD, environment, HOME, SHELL,
Herdr session, pane, or internal terminal ID.

The only server-side shell is `/bin/bash --noprofile --norc -i`, run after `fchdir` on
the trusted root descriptor with the closed `HOME`, `PATH`, `SHELL`, `TERM` environment.
The controller does not implement remote terminals, isolated worktrees, durable output,
or identity recovery.

Workspace and legacy PTYs have separate owner APIs. A Workspace PTY does not appear in
legacy listing or accept legacy controls, but counts toward the shared process limit. A
parent status-pipe setup/read/close failure kills, reaps, and closes the new PTY before it
can be registered. A missing output snapshot is distinct from a valid empty snapshot and
is never replayed as an empty history.

## HTTP And State

The Next-only controller owns the Workspace ticket collection, detail, create, interrupt,
reconnect, restart and close routes. G3 success is `{data,meta}`; malformed JSON/bodies
are G3 `invalid_argument` (400). Create/restart validate dimensions and all signed-64
revision/generation fences before Ticket, Operation, or PTY side effects.

Ticket projection is `{ticket,runtime}`. Runtime reports `state`, `replay_available`, and
`replay_truncated`; a persisted running ticket with no current boot binding is
`process_unknown`, never a recovered process. Create/control Operations are idempotent;
replay does not fork, signal, kill, resize, or make a Ticket/Operation write again. A new
control request validates its revision/generation before an Operation is created or
dispatched; a same-key replay returns its prior result even after that action changed the
Ticket revision. Reconnect rejects cursor exhaustion and commits its Ticket CAS before
resizing the PTY. Provider success/failure/unknown outcomes are recorded before returning.

An exact kill must be confirmed before its binding is discarded or a restart can create the
next generation. An unknown kill keeps cleanup ownership and reports `process_unknown`.
Natural exit is observed independently of a WebSocket, persists an opaque exit receipt with
`observed_state=stopped`, and remains `exited` after controller restart; it may then be
restarted or closed. Disconnect only releases a stream lease. Graceful controller shutdown
marks live desired tickets `recovery_required`; it is not a user close.

## Stream

The WS URL has no query parameters. Authorization failure closes `1008`. The first client
frame is exactly `{type:"attach",revision,generation,cursor}`. Server replay is exactly
`replay_start`, bounded binary history, `replay_complete`, then live binary output. Later
client frames are exact `input` or `resize` frames with the same fence. Unknown, binary,
stale, or invalid frames cause no PTY side effect.

Application close codes are only `4400` invalid input, `4404` scoped absence, `4409`
stale/taken-over/stopped, and `4503` authority, I/O, or process-unknown failures.

## Capability

In the Next profile, Workspace detail and Files responses derive `terminal.pty` from this
same authority matrix and controller readiness. Legacy and Next-off retain the prior
unavailable capability. The controller is closed before foundation stores during lifespan
shutdown.
