# Project Memory v1

## Boundary

`memory_store.py` owns a standalone, Project-scoped durable Memory store. It
owns current Fact heads, append-only Fact revisions, Candidate state and
decision receipts, one Project memory revision, and an append-only local
`memory_events` audit timeline. It does not own Project identity, Workspace,
Assignment, Operation, Runtime, the Domain Event Journal, Checkpoint, Context
Pack, Agent Mail, or source discovery.

`memory_api.install(app, MemoryApiService(store_provider))` is an injectable
read helper. It installs routes only on the explicitly supplied FastAPI app.
This car does not import or modify the shared server, create a runtime path,
install a producer, expose a write route, publish SSE, or claim that Memory is
available in the product shell.

## Facts And Candidates

A Fact is identified by `(project_id, fact_key)`. Its mutable head contains only
the current version, kind, status, and timestamps; its content is an
append-only revision. `append_fact` validates `expected_version` in one
`BEGIN IMMEDIATE` transaction, appends version `actual + 1`, advances the head,
increments the Project memory revision, and appends one local audit event.
Fact kind is immutable. Status is one of `current`, `stale`, `conflict`, or
`retired`.

A Candidate is Project-scoped and begins at revision 1 in `pending`. Candidate
creation binds the target Fact version and is idempotent by its complete
canonical request. A changed request with the same candidate ID fails closed.
Agents and Harnesses may propose Candidates; they cannot change current Facts.

`decide_candidate` accepts `decided_by` from a future trusted authentication
boundary. A request payload claiming that it is human is not authority. The
Store primitive checks the Project, candidate revision, state, and expected
Fact version in one `BEGIN IMMEDIATE`. `approve` applies the Candidate content,
`merge` applies explicit merged content, and `reject` writes no Fact. The
transaction then advances the Candidate to revision 2, appends an immutable
decision receipt, advances the Project revision, and appends one local audit
event. Any failure rolls back every change.

## Stored Values

Identifiers and timestamps are bounded and canonical. Timestamps use UTC
`YYYY-MM-DDTHH:MM:SSZ` or `YYYY-MM-DDTHH:MM:SS.ffffffZ`; offsets, spaces,
redundant zero fractions, and fractions other than exactly six digits are
invalid rather than normalized. SQLite integers stay in the signed 64-bit
range. Fact values and local event summaries are bounded to 16 KiB canonical
JSON objects containing only JSON values. Typed source, proposer, actor, and
decision references are exactly `{type,id}`. Materialized rows revalidate
identifiers, enums, versions, timestamps, JSON, candidate request digests,
Fact-head bindings, and Candidate-decision-result bindings; invalid persisted
data is `store_corrupt`. Candidate-create replay fully materializes and
validates the stored Candidate before comparing its request digest.

Memory values reject a fixed key enumeration after ASCII case folding at every
nested object depth, including objects inside lists. The exact enumeration is:

- credentials: `token`, `tokens`, `password`, `passwords`, `credential`,
  `credentials`, `secret`, `secrets`, `authorization`, `cookie`, `private_key`,
  and `api_key`;
- environment: `env`, `environment`, `env_dump`, `env_dumps`,
  `environment_dump`, and `environment_dumps`;
- terminal: `terminal_output`, `terminal_outputs`, `terminal_scroll`,
  `terminal_scrolls`, `scrollback`, `scrollbacks`, `terminal_scrollback`, and
  `terminal_scrollbacks`;
- file content: `file_body`, `file_bodies`, `file_content`, `file_contents`,
  `full_file_body`, `full_file_bodies`, `full_file_content`, and
  `full_file_contents`;
- message content: `message_body`, `message_bodies`, `full_message_body`, and
  `full_message_bodies`;
- reasoning: `reasoning` and `hidden_reasoning`.

This is an exact enumeration, not substring or heuristic matching. Memory
stores summaries, stable identifiers, and source references. Checkpoints and
Context Packs are entirely deferred.

`memory_events` is a local audit track committed atomically with Memory state.
It is not EVENT-001 and the Store never imports or calls the Domain Event
provider. A later integration car may idempotently project a `memory_event_id`
after commit, but it cannot claim cross-database atomicity.

## Store Lifecycle

`initialize(path)` is explicit. It builds and validates a unique private `0600`
SQLite leaf in the target directory, fsyncs it, and publishes without replacing
an existing destination. Failure removes only that invocation's temporary
leaf and leaves no partial final Store. `open_existing(path)` never initializes
or migrates.

Every open requires an owned regular `0600` leaf and a private parent. The v1
fingerprint covers every non-SQLite schema object plus table columns, indexes,
index columns, foreign keys, schema metadata, `user_version`, foreign-key
integrity, and `quick_check`. Missing, future, unknown, or drifted schema fails
closed. Reads use `mode=ro`, `query_only`, and an explicit transaction; the
Store owns no persistent connection and does not enable WAL.

All validation, SQLite, schema, and materialization failures expose a stable
`MemoryStoreError` without raw SQL, filesystem paths, payloads, or low-level
exception context. Connections close and failed write transactions roll back.

## Read API

The injectable helper exposes only:

- `GET /api/projects/{project_id}/memory/summary`
- `GET /api/projects/{project_id}/memory/facts`
- `GET /api/projects/{project_id}/memory/candidates`
- `GET /api/projects/{project_id}/memory/timeline`

Facts accept repeated `status`, `after_key`, and `limit`. Candidates accept
repeated `status`, `after_candidate_id`, and `limit`. Timeline accepts
`after_seq` and `limit`. Limits are canonical integers from 1 through 100;
cursors are exclusive and pagination is deterministic. Every Store query
requires the route's `project_id`; records from another Project never appear.

Success is a G3 `{data,meta}` envelope sourced from `project_memory`.
`memory.read` is available for the installed helper. `memory.write`,
`memory.checkpoint`, and `memory.contextPack` remain explicitly unavailable.
Errors are exactly
`{error:{code,message,retryable,request_id,details}}` and contain no private
Store or payload detail.
