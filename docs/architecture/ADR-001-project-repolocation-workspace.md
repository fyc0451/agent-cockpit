# ADR-001: Project, RepoLocation, and Workspace identity

- Status: accepted for Cockpit 2.0 reconstruction
- Baseline: `agent-cockpit-next@a38fb35`
- Product authority: the v2.5 reconstruction-ready prototype documents

## Decision

Cockpit 2.0 uses this durable ownership hierarchy:

```text
Project
  +-- RepoLocation (an existing canonical directory on one Runtime Node)
        +-- Workspace (a durable environment for one goal)
              +-- Run
              +-- AgentInstance / RuntimeGeneration
              +-- Terminal / Files / Git
              +-- Assignment / Checkpoint / Activity
```

Project memory, inbox, harness policy, and project activity belong to Project.
Herdr sessions and Coordination runs are runtime attachments; neither is a
durable Workspace identity. Agent Mail remains the authority for messages and
identities, but its `human_key` is not the Project Registry authority.

## Stable identity

- `project_id`, `repo_location_id`, and `workspace_id` are opaque and never
  recomputed from a path, display name, pane, or session.
- `project_slug` is immutable after creation and exists for URLs and legacy API
  compatibility.
- `(node_id, canonical_path)` is unique among active RepoLocations.
- A matching Git remote or fingerprint is only a possible-project hint. It
  never merges projects automatically.
- A non-Git directory may be registered, with Git/worktree capabilities marked
  unavailable.

## Project registration

The user selects a node, browses an allowlisted root, selects an existing
directory, reviews a server-side read-only discovery result, then confirms a
Project or RepoLocation registration. The browser never supplies an absolute
path, Git URL, default branch, or arbitrary worktree target.

Registration writes only the new registry. It does not create or modify an
Agent Mail project, Herdr session, Coordination run, Git branch, worktree, or
Agent.

## Workspace creation

A Workspace is created only under an existing Project and from one of its
registered RepoLocations. A plan chooses a server-observed base ref and an
isolation policy; execute later performs side effects through an Operation.
Project and RepoLocation remain valid if Workspace creation fails.

## Compatibility

Legacy Agent Mail, Herdr, and Coordination state is imported read-only and
idempotently into mapping records. The importer never modifies those stores or
user Git state. Local Project registration remains available when Agent Mail is
down; messaging is then reported as `available=false` with a reason.

## Consequences

The new registry owns only durable product membership, lifecycle, display
metadata, and optimistic versions. Existing authorities continue to own their
current runtime and communication data. New application services may wrap old
routes, but the rewrite must not create a second complete server or move
`server.py` wholesale.
