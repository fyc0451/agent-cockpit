# Runtime Provider v1

## Boundary

`LocalHerdrProvider` is a dormant, injected read adapter for the Local Herdr runtime.
It is not wired into `server.py` by this car. Its stable identity is:

```text
node_id=local
provider_id=local_herdr
protocol=1
```

The provider owns normalization of capabilities, handshake, session enumeration,
and per-session snapshots. The transport owns only calls to the local runtime. The
provider does not start or stop a process, create a PTY, attach an Agent, open a
terminal, recover a run, or read Project, Workspace, Operation, Event, or Memory
authority.

## Read Contract

`capabilities()` proves the provider ID, exact protocol, installation state, and
required `handshake`, `list_sessions`, and `snapshot` methods. Missing installation,
protocol mismatch, transport timeout, unavailable transport, and malformed source
data are distinct stable errors. A non-Local node fails closed.

`handshake()` never trusts a runtime-supplied identity by itself. Identity is
`verified` only when `(provider_id,node_id,runtime_identity,epoch)` exactly matches a
provider-owned observation. Without that proof it returns:

```json
{"runtime_identity": null, "identity_status": "identity_unverified", "epoch": null}
```

`list_sessions()` strictly normalizes session identity/status, then obtains one
snapshot per session. A failed or malformed individual snapshot produces one
unavailable session plus a stable `session_errors` item and marks the response
partial. A successful source with no rows is a true empty result (`sessions=[]`,
`empty=true`, no session errors); source failure is never converted to empty.
`snapshot()` is the provider aggregate snapshot and has the same normalized result.

Transport, source, and process state remain separate:

- transport: installed/protocol/timeout/connectivity and G3 error;
- source: available/partial plus warnings in `meta`;
- process: each session snapshot's `running`, `stopped`, or `unknown` value.

## Capabilities

All successful responses include the current capability snapshot. `runtime.read` is
available. These write/side-effect capabilities are always unavailable in v1:

```text
runtime.attach   -> operation_journal_required
runtime.terminal -> workspace_ticket_required
runtime.recovery -> operation_journal_required
```

An unverified identity never enables a write capability.

## Errors And Redaction

Errors use exactly the G3 five-field object:

```json
{"error":{"code":"...","message":"...","retryable":false,
          "request_id":"req_...","details":{}}}
```

The boundary never returns exception text, command output, tokens, terminal data,
absolute runtime/config/socket paths, or arbitrary transport fields. Stable codes are
`invalid_node`, `provider_not_installed`, `protocol_mismatch`, `transport_timeout`,
`transport_unavailable`, `source_malformed`, `store_read_failed`, `schema_missing`,
and `schema_mismatch`.

## Provider Observation Store

`runtime_provider_store.py` owns only schema metadata and one identity observation
watermark per `(provider_id,node_id)`. It cannot store Project, Workspace, Agent,
Operation, Event, Memory, terminal, token, or absolute runtime path data.

`initialize()` is the explicit, idempotent schema creation boundary.
`record_observation()` is an explicit owner write and atomically increments the
watermark. `open_existing()` and `get_observation()` use SQLite read-only mode; they
never initialize, migrate, create, or update the database. Missing or mismatched
schema fails closed rather than appearing as an empty observation.
