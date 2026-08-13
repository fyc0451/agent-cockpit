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

The accepted source schemas are exact, not required-column subsets:

- Agent Mail `PRAGMA table_info(projects)` is
  `id INTEGER NOT NULL PRIMARY KEY`, `slug VARCHAR(255) NOT NULL`,
  `human_key VARCHAR(255) NOT NULL`, `created_at DATETIME NOT NULL`, and nullable
  `archived_at DATETIME`, in that order.
- Coordination `PRAGMA table_info(runs)` is `run_id TEXT PRIMARY KEY`, followed by
  non-null `project_key TEXT`, `session TEXT`, `session_dir TEXT`, `revision INTEGER`,
  `state TEXT`, `config_hash TEXT`, `started_ts REAL`, and nullable `closed_ts REAL`,
  in that order. The authority DDL also has `UNIQUE(session, session_dir, revision)`;
  that table-level index is not exposed by `PRAGMA table_info` and is outside this
  fingerprint check.
- `mail-projects.json` has exactly `version` and `sessions`; `version` is an integer
  (a JSON boolean is not an integer) equal to `1`, and `sessions` is an object.
- A Herdr session has exactly `session`, `session_dir`, `version`, and `workspaces`;
  `version` is an integer equal to `3`. Each workspace has exactly `workspace_id` and
  `identity_cwd` string fields.

Extra, missing, reordered SQLite columns, changed declared types/constraints, unknown JSON
fields, future versions, and wrong JSON container types are `source_corrupt`. A valid empty
sessions object/list of Herdr workspaces remains distinct from malformed input.

## Deterministic provenance

`source_key = "sha256:" + sha256(canonical_json(identity))` and
`source_digest = "sha256:" + sha256(canonical_json(evidence))`, where `canonical_json`
delegates to the accepted Registry domain canonicalizer: sorted keys, `(",",":")`
separators, ASCII, `allow_nan=False`, and non-string mapping keys rejected. Unordered rows
(Herdr workspaces) are sorted by `(workspace_id, identity_cwd)` before hashing.

Provenance identity is the full `(source_kind, source_key)` pair. A candidate may retain
multiple distinct keys of the same kind; its complete source tuple is sorted by that pair.
Repeating the same pair with the same digest is idempotent. Repeating it with different
digests is `evidence_conflict`: the source is `error`, affected paths produce no candidate,
and the Store is not called for them. Source/row enumeration order and JSON key order do not
change the candidate or Store input. When multiple Agent Mail identities name one exact path,
candidate metadata is selected by the same stable identity order rather than query order.

## Candidate model

Candidates are grouped by **exact canonical local path only**. Ownership sources
(`agent_mail_project`, `mail_projects_session`, `coordination_run`) create candidate
groups. Herdr is **observational**: it attaches a `herdr_session` binding to an existing
candidate whose path matches a workspace `identity_cwd`. Herdr alone creates no candidate
(A04). A Herdr session spanning more than one candidate path is ambiguous and attaches to
**none** (it must not bind one `herdr_session` key — `UNIQUE(source_kind, source_key)` — to
multiple projects, and must not arbitrarily pick the first path).

`(session, session_dir)` is the exact session-generation relation shared by Mail,
Coordination, and Herdr. Before creating groups, all available Mail/Coordination ownership
paths and a Herdr generation with one unambiguous workspace path must agree. More than one
path for a generation is `authority_disagreement`: every participating source is `error`,
the conflicting generation's paths produce no candidate, Herdr evidence is not attached,
and the Store receives zero calls for those candidates. The generation key is never reduced
to session name alone.

No candidate may be merged by basename, Git remote, display name, session name, or pane cwd.
Invalid paths (relative, root, trailing/`//`/`.`/`..` components, NUL) and paths outside the
configured `local_boundary` are skipped (`unverified_path`); the importer does not call
`Path.resolve()` across an unbounded gap.

## Source availability (not false-empty)

A missing optional source is `unavailable`; a corrupt/future-schema/truncated source is
`error`; a valid empty source is `ok` with count 0. The three are distinct: empty valid
sources must not be reported `unavailable`. Any source `error` makes `ImportReport.complete`
`False`. Normalization errors `evidence_conflict` and `authority_disagreement` are also
source errors. Unaffected exact candidates may still import or replay, but the report never
claims a complete scan.

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
merge), complete same-kind provenance, generation agreement, strict source schemas,
deterministic Store inputs, per-candidate single Store call, and idempotent replay. Sensitive
path/adversarial, leakage, runtime-state, and authority-immutability gates are owned and run
separately by Claude/Kimi reviewers.
