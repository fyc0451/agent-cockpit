# Project Registry API v1

## Boundary

This Local-only API exposes Project Registry and read-only discovery without changing
legacy `/api/projects/{slug}` or `/api/projects/{slug}/workbench`. It owns no SQL,
filesystem traversal, Git operation, Mail, Herdr, Coordination, Workspace, or remote
runtime side effect. `project_registry_api.py` uses only the accepted Registry Store
primitives and injected Local discovery service.

## Routes

```text
GET  /api/runtime-nodes
GET  /api/runtime-nodes/{node_id}/roots
GET  /api/runtime-nodes/{node_id}/directories?root_id=&path=&query=
POST /api/project-discovery
GET  /api/project-registry/projects?lifecycle=active|archived&limit=1..100&cursor=
POST /api/project-registry/projects
GET  /api/project-registry/projects/{project_id}
GET  /api/project-registry/projects/{project_id}/repo-locations
POST /api/project-registry/projects/{project_id}/repo-locations
```

New Registry routes only accept opaque `project_id`; they never interpret a slug as
an ID. `cursor` is a versioned opaque token bound to lifecycle and the Store's final
visible project anchor. It is not a Project ID and a malformed or filter-mismatched
cursor is `invalid_argument`.

## Envelopes and redaction

Every 2xx response is `{data, meta}`. `meta` has fresh `request_id`, `generated_at`,
`partial`, structured source status, and capability objects. Every error is exactly
`{error:{code,message,retryable,request_id,details}}`. New endpoints do not use legacy
`detail` responses.

`server.py` keeps a scoped G3 bridge for the nine routes only. Auth middleware early
returns, `HTTPException`, `RequestValidationError`, and unhandled exceptions on those
paths share the same request-scoped `request_id` and the five-field error object.
Legacy `/api/projects/{slug}` and other existing APIs keep their `{detail:...}` shape.

Public Project/RepoLocation DTOs never include canonical filesystem path, root absolute
path, Git remote URL, Git stderr, Store exception text, request body, or idempotency
key. Create, attach, and receipt replay must explicitly project `_project` /
`_location` fields and must not forward a Store receipt as-is. Discovery preserves
its existing safe locator/display/path-digest/VCS DTO. Remote Herdr, Memory,
Automation, Browser, GitHub, and Electron remain explicitly unavailable. Registry
read and write capability are independently reported.

## Reads and discovery

Registry list/detail/location reads use Store snapshots. List order is immutable
`project_id`; location order is immutable `repo_location_id`. Reads do not initialize
or mutate Registry state. Local discovery routes use the injected Local-only provider;
non-Local nodes are `capability_unavailable`, never a fallback to Local.

## Registration and idempotency

Create accepts exactly `display_name`, `slug`, `goal`, `locator`, and
`expected_discovery_fingerprint`. Attach accepts exactly `locator`,
`expected_discovery_fingerprint`, and `expected_project_version`. Both POST routes
require `Idempotency-Key` and use these scopes:

```text
project-registry.projects.create.v1
project-registry.repo-locations.create.v1
```

The API first preflights `(scope,key,canonical body digest)`. Exact receipt replay
returns its original status/data without a second discovery. A mismatched body returns
`idempotency_conflict`. Only a receipt miss does a second full discovery; incomplete
evidence, warnings, or missing Registry source fail closed. A changed fingerprint is
`discovery_stale` with no Registry write. An exact active owner is
`location_already_registered`; possible Project matches are advisory only.

Trusted discovery canonical path, VCS kind, and remote fingerprint are passed to the
Store's atomic registration/attach primitive. Attach requires Git plus a non-null remote
fingerprint and Store-side active Project/version/remote-proof validation. No route
creates a Workspace, Agent, Run, pane, branch, worktree, or Mail identity.

## Stable HTTP errors

400: `invalid_argument`, `invalid_locator`, `idempotency_key_required`; 401:
`unauthenticated`; 403: `forbidden`, `root_forbidden`; 404: `node_not_found`,
`project_not_found`; 409:
`discovery_stale`, `project_slug_conflict`, `location_already_registered`,
`version_conflict`, `repository_identity_unproven`, `idempotency_conflict`; 412:
`capability_unavailable`; 503: discovery/Store availability or schema safety errors;
unknown failures are `500 internal_error`. Only transient discovery and Store I/O errors
are retryable.
