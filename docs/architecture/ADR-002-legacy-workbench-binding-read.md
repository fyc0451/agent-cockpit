# ADR-002: Legacy workbench compatibility uses persisted provenance

- Status: accepted for the first compatibility slice
- Product authority: the v2.5 reconstruction-ready Project and Workspace contract
- Depends on: ADR-001

## Context

The legacy `GET /api/projects/{slug}/workbench` route still reads Agent Mail,
Coordination, and live Herdr state. Cockpit 2.0 must bind that response to the
new durable Project identity without making a second authority or mutating any
legacy store.

The importer persists only hashed provenance identity in
`legacy_project_bindings`. A raw `(session, session_dir)` cannot be recovered
from a binding row. The compatibility service can, however, canonicalize the
observed identity, compute the same source key, and compare it with the
immutable binding set for the Registry Project.

## Decision

1. A Store-owned prerequisite car adds
   `list_legacy_bindings(project_id)`. It uses one `mode=ro`, `query_only`
   transaction, returns immutable `LegacyBindingRecord` values sorted by
   `(source_kind, source_key)`, and performs no migration or write.
2. The compatibility service resolves the Registry Project by immutable slug,
   computes the importer-defined SHA-256 source key for each exact
   `(session, session_dir)` observation, and attaches a live session only when
   the matching `herdr_session` or `mail_projects_session` binding belongs to
   that Project. A session name alone is never sufficient.
3. The route retains its existing four-key legacy response and legacy error
   envelope. Agent Mail unavailable or unreadable remains a stable `503` for
   this compatibility route. This does not limit the independent Registry API:
   Project and RepoLocation reads and registrations remain available without
   Agent Mail, while messaging is a separate unavailable capability.
4. The compatibility car does not parse persisted Herdr files, query Registry
   SQL directly, create or repair bindings, import legacy data, or add
   Workspace write behavior.

## Consequences

The Store and compatibility scopes remain independently reviewable. Imported
provenance, rather than a live naming heuristic, decides durable Project
membership. A later product car may redesign the public workbench response to
return partial blocks when Mail is down; that is not a backward-compatible
change and is outside this slice.
