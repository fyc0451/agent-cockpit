# Project Legacy Import v1

> PROJ-003. Read-only orchestration that copies only identity/provenance needed to
> relate legacy sources to the isolated Project Registry.

## Boundary

`project_legacy_import.py` is a service layer. It **never** opens the Registry via raw
SQL — all registry writes go through the accepted Store primitive
`ProjectRegistryStore.import_legacy_project` (single `BEGIN IMMEDIATE`, owned by PROJ-003A).
It **never** mutates any legacy authority, and **never** creates Workspace/Run/Agent/pane/Git
state. The importer is independent of PROJ-002 (Local discovery) and PROJ-004 (API):
it consumes already-canonical local paths from legacy evidence, sets `vcs_kind="none"`,
and does not claim Git facts.

## Sources and authority

| Source | `source_kind` | Remains authoritative for | Stable identity | Canonical path field |
|---|---|---|---|---|
| Agent Mail `projects` | `agent_mail_project` | mail project, messages, identities | `{"project_id": <id>}` | `human_key` |
| `mail-projects.json` | `mail_projects_session` | session↔mail binding | `{"session","session_dir"}` | `project` |
| Herdr persisted session | `herdr_session` | session/workspace/pane/runtime | `{"session","session_dir"}` | workspace `identity_cwd` |
| Coordination `runs` | `coordination_run` | run/participant/assignment state | `{"run_id"}` | `project_key` |

The importer copies only the normalized evidence above. It does **not** copy messages,
agents, pane/layout, participants, assignments, terminal output, or mutable runtime status.
`Agent Mail`/`Coordination` are read `mode=ro`; JSON is parsed strictly (duplicate keys
rejected; `mail_projects._load` is intentionally not used because it silently converts
corruption into an empty store).

## Deterministic provenance

`source_key = "sha256:" + sha256(canonical_json(identity))` and
`source_digest = "sha256:" + sha256(canonical_json(evidence))`, where `canonical_json`
uses sorted keys, `(",",":")` separators, ASCII, `allow_nan=False`. Unordered rows
(Herdr workspaces) are sorted by `(workspace_id, identity_cwd)` before hashing. Enumeration
order and JSON key order do not change key/digest.

## Candidate model

Candidates are grouped by **exact canonical local path only**. Ownership sources
(`agent_mail_project`, `mail_projects_session`, `coordination_run`) create candidate
groups. Herdr is **observational**: it attaches a `herdr_session` binding to an existing
candidate whose path matches a workspace `identity_cwd`. Herdr alone creates no candidate
(A04). A Herdr session spanning more than one candidate path is ambiguous and attaches to
**none** (it must not bind one `herdr_session` key — `UNIQUE(source_kind, source_key)` — to
multiple projects, and must not arbitrarily pick the first path).

No candidate may be merged by basename, Git remote, display name, session name, or pane cwd.
Invalid paths (relative, root, trailing/`//`/`.`/`..` components, NUL) and paths outside the
configured `local_boundary` are skipped (`unverified_path`); the importer does not call
`Path.resolve()` across an unbounded gap.

## Source availability (not false-empty)

A missing optional source is `unavailable`; a corrupt/future-schema/truncated source is
`error`; a valid empty source is `ok` with count 0. The three are distinct: empty valid
sources must not be reported `unavailable`. Any source `error` makes `ImportReport.complete`
`False`; already-imported exact candidates may still replay, but the report never claims a
complete scan.

## Orchestration and replay

`import_legacy(store, roots)` reads each source read-only, builds candidates, and calls
`store.import_legacy_project(...)` **exactly once per candidate**. Atomicity and idempotency
are owned by the Store: exact replay returns the original IDs, adds no rows, does not change
timestamps, and sets `replayed=True`; a digest/path/slug conflict produces zero writes with a
stable code. The importer performs **no** cross-candidate rollback — append-only provenance
makes deleting earlier candidates invalid. A crash may leave earlier candidates committed but
never a half candidate; rerun converges through exact replay.

## Rollback semantics (Delivery)

Reverting this car means disabling/reverting the importer code and restoring the isolated
Next Project Registry snapshot if imported rows must be removed. It **never** deletes or
updates append-only `legacy_project_bindings` rows and **never** mutates a legacy authority.

## Test scope

`tests/test_project_legacy_import.py` covers ordinary functional behavior: frozen
source key/digest vectors, source availability separation, exact-path grouping (no heuristic
merge), per-candidate single Store call, idempotent replay, path safety, and read-only
authority immutability. Adversarial/security harnesses are owned by separate reviewers.
