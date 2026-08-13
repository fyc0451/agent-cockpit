# Domain Event Journal v1

## Boundary

`event_store.py` owns only a standalone durable event journal. It records what happened;
it does not replace Project, Workspace, Assignment, Operation, Runtime, Memory, Mail, or
any other business authority. This car creates no Activity, Inbox, Search, SSE, worker,
server wiring, shared schema, runtime path, or projection.

## Envelope

`append` accepts exactly `event_id`, `event_type`, `event_version`, `project_id`,
`workspace_id`, `aggregate_type`, `aggregate_id`, `aggregate_version`, `actor`, `source`,
`correlation_id`, `causation_id`, `occurred_at`, `payload`, and `receipt_refs`.
`actor` is exactly `{type,id}`; `source` is exactly `{type,source_event_id}`; every receipt
is exactly `{type,id}`. `event_version` and `aggregate_version` are positive JSON integers,
not booleans. `recorded_at` and a journal-monotonic positive `cursor` are assigned by Store.

`(source.type, source.source_event_id)` is the external idempotency identity. Exact canonical
replay returns the first immutable event and cursor; a changed envelope returns
`event_dedup_conflict`. Event ID collision also fails closed. Reads return immutable records.

Payload is bounded to 16 KiB canonical JSON and may contain only JSON values. It rejects
`secret`, credentials/tokens/passwords, terminal output or scrollback, hidden reasoning,
complete file body/content, and complete message body fields at every nesting level.
Receipt refs are stable identifiers only; they do not copy receipt payloads.

## Store and Reads

`initialize(path)` is explicit and creates a private `0600` SQLite leaf. The Store validates
an owned v1 fingerprint on every open. Missing Store reads return `schema_missing` without
creating a DB; unknown/future/drift schema fails closed. A live read uses `mode=ro`,
`query_only`, and an explicit snapshot; it never initializes, migrates, enables WAL, or writes.

`list(project_id, workspace_id, after_cursor, types, limit)` requires project scope, supports
only a stable type tuple and `1..100` limit, orders by ascending cursor, and returns the last
visible cursor only when another matching row exists. `get(event_id)` is exact event-ID read.

All SQLite/materialization failures expose only a stable `EventStoreError` code with no cause,
context, raw SQL, path, payload, or low-level detail. Connections always close.

## API

`event_api.install(app, EventApiService(store_provider))` is an injectable G3 helper only.
It installs `GET /api/events/{event_id}` on an explicitly supplied app; this car does not
import or modify the shared server. Success is `{data,meta}` and error is exactly
`{error:{code,message,retryable,request_id,details}}`.
