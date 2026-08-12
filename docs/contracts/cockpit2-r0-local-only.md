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
#/settings/doctor
#/projects/:projectId/workbench
#/projects/:projectId/workspaces/:workspaceId
#/projects/:projectId/workspaces/:workspaceId/files
#/projects/:projectId/workspaces/:workspaceId/terminal
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
    "partial": false,
    "sources": [
      {
        "id": "project_registry",
        "available": false,
        "freshness": "unavailable",
        "reason": "not_implemented"
      }
    ]
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

The new Project phase adds only the contracts below before Workspace side
effects:

```text
GET  /api/runtime-nodes
GET  /api/runtime-nodes/{nodeId}/roots
GET  /api/runtime-nodes/{nodeId}/directories
POST /api/project-discovery
GET  /api/projects
POST /api/projects
GET  /api/projects/{projectId}
GET  /api/projects/{projectId}/repo-locations
POST /api/projects/{projectId}/repo-locations
GET  /api/projects/{projectId}/workspaces
GET  /api/projects/{projectId}/workspaces/{workspaceId}
```

Local directory endpoints accept `root_id + relative path`; they reject an
absolute path. Retriable POSTs require `Idempotency-Key`, and aggregate writes
use expected versions. Project registration never creates a Workspace, Agent,
run, pane, branch, or worktree.

## Writer concurrency

W1 retains the machine-enforced limit of two writers: one bounded backend car
and `WEB-001`. Reviewer and analyst work is read-only. Raising the limit is a
separate delivery car that must first prove module, Store, migration, and API
ownership in tests. The intended ceiling is three writers after the Project
boundary is accepted and four only after Runtime/Event/Memory boundaries no
longer share Store or entrypoint hotspots.

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
