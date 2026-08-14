# Cockpit 2 Foundation Wiring v1

## Runtime stores

The Next profile owns five private SQLite leaves. Each entry is rooted at
`data`, is a `file`, has writer `server`, and requires exact mode `0600`:

| Key | Leaf |
| --- | --- |
| `runtime_provider` | `runtime-provider.sqlite3` |
| `event_journal` | `event-journal.sqlite3` |
| `operation_journal` | `operation-journal.sqlite3` |
| `project_memory` | `project-memory.sqlite3` |
| `terminal_ticket` | `terminal-ticket.sqlite3` |

Path resolution and readiness probes are pure until an explicit lifecycle
initializer runs. Schema probes return `missing_creatable` before importing a
store module and call only that store's read-only `open_existing` validator.
They never call `initialize` and never create a database or SQLite sidecar.

## Inventory compatibility

The snapshot writer emits inventory schema v3 with exactly 21 entries, 11
SQLite stores, and 18 snapshot stores. Readiness keeps the immutable v1 catalog
at 15 entries and v2 at 16 entries, and accepts only the exact v1, v2, or v3
catalog. Schema evidence remains version 1.

## Next lifecycle

Foundation wiring is absent when the Next profile is off. When enabled,
startup requires the Next instance lock, prepares Project Registry, resolves
and validates all five paths, then initializes stores in this fixed order:

1. Runtime observation
2. Event Journal
3. Operation Journal
4. Project Memory
5. Terminal Ticket

The stores remain local until all five succeed. Publication is one assignment
of a single immutable bundle pointer; readers capture that pointer once. Any
initializer or later startup failure first clears publication and then closes
opened stores in reverse order. Shutdown does the same in an outer `finally`;
one close error is logged but cannot prevent remaining closes. Published files
are retained for an idempotent retry.

Only Event, Operation, Memory, and Terminal Ticket accepted read APIs are
installed. Their providers never initialize on request and return sanitized
`503` before startup or after shutdown. No write route is installed.

Runtime is observation storage only. This wiring does not create a Runtime
transport, Runtime HTTP route, provider adapter, node identity, or verified
runtime identity. Existing `/api/runtime-nodes` behavior is unchanged.
`PUBLIC_PATHS` is unchanged, so all new API routes remain under existing auth.
