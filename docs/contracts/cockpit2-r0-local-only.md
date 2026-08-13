# Cockpit 2.0 R0 Local-only contract

This contract freezes the first usable reconstruction slice. The production
service on `127.0.0.1:8790` remains frozen. Development and verification use
only the isolated Next profile and `127.0.0.1:18790`.

## Route and ownership

The first real journey is:

```text
Doctor -> Project -> Workbench -> Local Workspace -> Files or Terminal
       -> Assignment or Inbox -> Review or Operation
```

Hash routes carry stable identities and survive refresh:

```text
#/settings?view=doctor
#/projects/:projectSlug/workbench
#/projects/:projectSlug/workspaces/:workspaceId
#/projects/:projectSlug/workspaces/:workspaceId/files
#/projects/:projectSlug/workspaces/:workspaceId/terminal
```

The Web shell may render these routes before all backends exist, but it must
not invent Project or Workspace records. Missing capabilities are explicit and
non-actionable.

## Shared response contracts

Every source-backed view distinguishes data from source state:

```json
{
  "data": null,
  "meta": {
    "request_id": "req-...",
    "generated_at": "2026-08-13T03:00:00+08:00",
    "sources": [
      {
        "name": "project_registry",
        "status": "unavailable",
        "observed_at": null,
        "reason": "not_implemented"
      }
    ],
    "capabilities": {
      "remote_herdr": {
        "available": false,
        "reason": "deferred_after_local_core"
      }
    }
  }
}
```

The typed client preserves HTTP status, stable error code, `request_id`, and
details. It must not convert a failed source into an empty list or a successful
toast. At minimum the UI has distinct normal, loading, empty, partial, stale,
conflict, forbidden, error, and offline states.

Capabilities use this minimum shape:

```json
{
  "id": "remote_herdr",
  "available": false,
  "reason": "deferred_after_local_core"
}
```

The first slice must report Remote Herdr, Memory/Context Pack,
Automation/Browser, GitHub/PR, and Electron integration as unavailable. It must
not expose a write control that can appear to succeed for those capabilities.

## Minimum Local API surface

Existing read authorities remain valid while new adapters are introduced:

- health/Doctor: `/health/live`, `/health/ready`, and existing environment and
  Herdr probes;
- legacy project/workbench: existing project list and
  `/api/projects/{slug}/workbench` during compatibility;
- Assignment: existing Coordination Assignment read API and CAS writes;
- Files: existing allowlisted roots/tree/search/read API, later wrapped by a
  Workspace-scoped facade;
- Terminal: existing Local Herdr/PTY API, later requiring a Workspace-scoped
  ticket;
- events: existing SSE is an invalidation channel, not a second authority.

The new Project phase keeps the existing `/api` prefix and adds only the
contracts below before Workspace side effects. Project Registry reads use a
distinct resource path because legacy `/api/projects/{slug}` remains active:

```text
GET  /api/runtime-nodes
GET  /api/runtime-nodes/{nodeId}/roots
GET  /api/runtime-nodes/{nodeId}/directories
POST /api/project-discovery
GET  /api/project-registry/projects
POST /api/project-registry/projects
GET  /api/project-registry/projects/{projectId}
GET  /api/project-registry/projects/{projectId}/repo-locations
POST /api/project-registry/projects/{projectId}/repo-locations
GET  /api/project-registry/projects/{projectId}/workspaces
GET  /api/project-registry/projects/{projectId}/workspaces/{workspaceId}
```

Local directory endpoints accept `root_id + relative path`; they reject an
absolute path. Retriable POSTs require `Idempotency-Key`, and aggregate writes
use expected versions. Project registration never creates a Workspace, Agent,
run, pane, branch, or worktree.

New APIs use only opaque `project_id`; immutable `project_slug` remains the
browser route and legacy compatibility key. The server never accepts a token
that may ambiguously mean either one.

## Safety car order

Before Project implementation, the single backend writer completes these cars
in order:

```text
FD-001 -> RUNTIME-001 -> SEC-001 -> SEC-002
```

`FD-001` deterministically closes application-owned SQLite connections while
preserving transaction semantics. Its tests disable garbage collection and
prove descriptor counts stay stable using only temporary databases. Profile
lock and trust-boundary work must not hide or defer this prerequisite.

## Writer concurrency

W1 starts from a machine-enforced capacity of two active writer cars: one
bounded backend car and `WEB-001`. Reviewer, analyst, and test-only work does
not consume writer capacity. Two is the fail-closed baseline, not a permanent
limit; capacity changes only through accepted Delivery gates and must never be
raised by editing a number manually.

`DELIVERY-002-wip3-gate` is the only path from two to three writers. It first
proves Project Store, migration, API, and module ownership with non-overlapping
car scopes and negative delivery fixtures. `DELIVERY-003-wip4-gate` is a
separate path from three to four writers after the Local slice; it first proves
that Operation, Runtime Provider, Event, and Memory ownership no longer shares
Store migrations or entrypoint hotspots. Each gate changes the validator and
plan schema only for the initial schema-v2 transition; later gates extend the
validated capacity chain without rewriting the validator. A rejected or
reverted gate leaves the previous accepted capacity in force. All potentially
parallel cars remain subject to machine-checked non-overlapping scope ownership.

## R0 acceptance

- Hash deep links restore the same Project and Workspace selection.
- A source failure renders partial/error state rather than false empty data.
- The shell builds and tests independently without extending
  `static/index.html`.
- Laptop and narrow layouts retain Project, Workspace, and search navigation.
- Keyboard focus, dialog, and tab semantics are testable.
- Local/Remote location is always visible; only Local is enabled.
- Files and Terminal full-screen transitions preserve their resource identity.
- The E2E suite uses isolated fixtures or the Next profile, never production
  roots or demo success data.

## Deferred work

Workspace creation, file/Git writes, runtime restart, recovery, and other
multi-step side effects require an Operation journal with plan/execute,
idempotency, receipts, and recovery. Remote Herdr, Memory, Automation, Browser,
GitHub, and Electron extend the Local core through providers; they do not
replace it.
