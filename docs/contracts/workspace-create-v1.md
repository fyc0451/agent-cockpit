# Workspace Create v1

## Scope

This contract creates one persistent shared Workspace record for an existing
active Project and its active Local, available RepoLocation. It does not create
a worktree, checkout, branch, Herdr session, pane, Agent, Run, or runtime
resource. `isolated_worktree` and `review_detached` remain deferred until a
separate worktree-authority contract exists.

## HTTP contract

```text
POST /api/project-registry/projects/{project_id}/workspaces
Idempotency-Key: <required opaque key>

{
  "repo_location_id": "loc_<32 lowercase hex>",
  "name": "display name, 1..256 characters",
  "goal": "optional text up to 4096 characters" | null,
  "isolation_kind": "shared"
}
```

The body is strict: missing or unknown fields are rejected. Clients provide
only `repo_location_id`; they cannot provide a path, locator, canonical path,
workspace ID, or runtime identity. `name` is a display value and is never used
for path construction.

Success and exact replay return HTTP 201 with the original complete G3 response
body, including its original `meta`. Replay is byte-equivalent to the first
body. The `data` field uses the existing WorkspaceSummary shape:

```text
workspace_id, project_id, repo_location_id, name, goal, isolation_kind,
lifecycle, active_run_id, version, created_at, updated_at, repo_location
```

`repo_location` contains only `node_id` and `availability`. Responses and
errors never expose `canonical_path`.

## Preconditions

The path Project must exist and be active. The selected RepoLocation must be
owned by that Project, active, `node_id="local"`, and
`availability="available"`. Archived Projects and missing Projects both return
`project_not_found`; missing, cross-Project, or archived locations return
`repo_location_not_found`.

## Idempotency and persistence

The global scope is `project-registry.workspaces.create.v1`. Identity is
`(scope, Idempotency-Key, digest({project_id, ...strict_body}))`, explicitly
binding the URL Project into the digest. A matching replay returns the
original status and complete G3 body. Reusing a key with another body returns
`idempotency_conflict`, including across Projects. The body includes the
globally unique, Project-bound `repo_location_id`, but the implementation does
not rely on that uniqueness alone. Project and RepoLocation ownership checks
run before replay, then the Project-bound digest prevents cross-Project replay.

The store atomically appends one `workspaces` row and one
`idempotency_records` row. It does not write any runtime or coordination state.

## Stable errors

| HTTP | Code | Meaning |
| --- | --- | --- |
| 400 | `idempotency_key_required` | Missing or invalid Idempotency-Key |
| 400 | `invalid_argument` | Invalid path ID, strict body, or field shape |
| 400 | `unsupported_isolation_kind` | Isolation kind is not `shared` |
| 404 | `project_not_found` | Project is missing or archived |
| 404 | `repo_location_not_found` | RepoLocation is missing, cross-Project, or archived |
| 409 | `repo_location_not_local` | RepoLocation is not Local |
| 409 | `repo_location_unavailable` | RepoLocation is not available |
| 409 | `workspace_name_conflict` | Another active Workspace has the same Project/name |
| 409 | `idempotency_conflict` | Idempotency-Key was used with a different body |

## Rollback

Rollback reverts the route and store wrapper code. Workspace and idempotency
records are append-only and must not be deleted or updated. If data restoration
is required, restore an isolated Registry snapshot through an explicit
operations procedure; never perform per-row rollback.
