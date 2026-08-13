# Cockpit 2 Local Read-only Slice v1

## Scope

This contract adds the first persisted Workspace to local Files read path. It
does not change the legacy `GET /api/projects/{slug}/workbench` response, and it
does not add terminal, write, POST, or WebSocket behavior.

## Routes

All new routes return G3 `{data, meta}` success envelopes and G3 `error`
envelopes:

```text
GET /api/project-registry/projects/{project_id}/workspaces
GET /api/project-registry/projects/{project_id}/workspaces/{workspace_id}
GET /api/project-registry/projects/{project_id}/workspaces/{workspace_id}/files?path=<relative>
GET /api/project-registry/projects/{project_id}/workspaces/{workspace_id}/files/content?path=<relative>
GET /api/project-registry/projects/{project_id}/workspaces/{workspace_id}/files/search?path=<relative>&q=<query>&limit=<1..100>
```

Workspace list returns `project_not_found` for an unknown Project. Workspace
detail and every Files route return `project_or_workspace_not_found` for an
unknown Project, unknown Workspace, or a Workspace owned by another Project.
These cases are intentionally indistinguishable.

## Workspace Projection

A Workspace is returned only from the persisted Project Registry. Its public
DTO contains:

```text
workspace_id, project_id, repo_location_id, name, goal, isolation_kind,
lifecycle, active_run_id, version, created_at, updated_at,
repo_location: {node_id, availability}
```

The projection never contains `canonical_path`, `cwd`, `human_key`, or another
internal root representation.

## Local Files

Files access first resolves this authority chain:

```text
Project -> persisted Workspace -> active RepoLocation
        -> node_id=local -> availability=available -> internal canonical root
```

The browser supplies only a relative POSIX path. The empty string denotes the
Workspace root. Non-empty paths reject absolute paths, `~`, backslashes, empty
segments, `.` and `..` segments, control characters, and resolution outside the
trusted Registry root. Public tree, content, and search paths remain relative
to the Workspace root. Responses omit absolute paths and all modifiable/write
fields.

The Files routes are read-only and reuse the existing Files traversal, text
detection, size limits, and search limits. Search `limit` is a canonical base-10
integer from 1 through 100.

## G3 Metadata

Workspace metadata declares `project_registry`. Files metadata declares both
`project_registry` and `local_files`. Workspace-scoped capabilities include:

```json
{
  "files.read": {"available": true, "reason": null},
  "terminal.pty": {
    "available": false,
    "reason": "workspace_terminal_ticket_deferred"
  }
}
```

Workspace list has no selected Workspace and therefore reports `files.read`
unavailable with `workspace_selection_required`. Workspace detail reports it
available only when both Workspace and RepoLocation are active and the location
is local and available. Stable unavailable reasons are `workspace_not_active`,
`repo_location_not_active`, `repo_location_not_local`, and
`repo_location_unavailable`. Successful Files routes report it available.

Terminal remains unavailable. This slice performs no PTY operation, file
write, POST, or WebSocket connection.
