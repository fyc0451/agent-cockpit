# Operation Journal v1

Status: frozen provider contract for `OPERATION-001-journal`.

## Boundary

This slice is a durable but dormant journal. It stores plans and evidence; it
does not execute or inspect any domain. In particular it does not import or call
Runtime, Git, Files, Event, Memory, Registry, Coordination, or Delivery owners.
It has no shared runtime path, readiness registration, worker, scheduler, or
startup wiring.

The only public route is:

```text
GET /api/operations/{operation_id}
```

There is deliberately no public create, execute, retry, reconcile, list, SSE,
or fake `202` endpoint. A later wiring car may install this provider and a later
domain-owner car may add execution after durable workers exist.

## Store lifecycle

The caller injects an absolute SQLite path. `initialize(path)` is the only
operation allowed to create the private `0700` parent and exact `0600` database.
`open_existing(path)` and all reads fail closed and never create or migrate a
store. Existing symlinks, non-regular files, non-owner files, non-`0600` modes,
multiple hard links, sidecars, unknown schema objects, migration drift, old or
future versions, corruption, and unsafe parent directories are rejected.

Schema v1 consists of `operations`, `operation_preconditions`,
`operation_steps`, `operation_attempts`, `operation_receipts`, and
`schema_migrations`. The migration and receipt ledgers are append-only. The
entire `sqlite_master` object set, migration ID, version, and digest form the
strict schema fingerprint.

## Identity and idempotency

`operations` has unique `(scope, idempotency_key)`. Creation computes a
canonical SHA-256 digest over the URL/resource identity, immutable operation
fields, ordered fences and steps, and the strict request body. The same key and
digest returns the existing operation without another mutation. A different
digest returns `idempotency_conflict` and changes nothing.

Operation identity, kind, subject, plan digest, request digest, fence ordinals,
step ordinals, and step IDs are immutable after creation. Generation, epoch,
revision, and digest fences are typed opaque values saved for domain owners;
the Journal never reads a domain to evaluate them.

## State and CAS

Every mutation requires `expected_operation_revision` and executes under one
`BEGIN IMMEDIATE` transaction. A successful mutation increments the operation
revision exactly once. Step mutations also compare and increment the step
revision. Stale revisions, illegal transitions, and active-attempt conflicts
produce zero state change.

An outcome mutation that may settle locally prepared siblings must carry the
expected revision for every such sibling step. The exact step-ID set and every
revision are compared before any receipt or state change; missing, extra, or
stale entries roll back the entire transaction.

Allowed operation transitions are:

```text
planned          -> waiting_approval | running | failed
waiting_approval -> running | failed
running          -> succeeded | failed | compensating | needs_attention
compensating     -> compensated | needs_attention
```

`succeeded`, `failed`, `compensated`, and `needs_attention` cannot be left by
this car. A failed operation requires `failure_code`; `needs_attention`
requires `attention_reason`.

## Attempts and receipts

Preparing an attempt persists a globally stable `step_execution_id` before any
provider may be called. Attempts use mode `execute` or `compensate` and state
`prepared`, `dispatched`, `succeeded`, `failed`, or `outcome_unknown`. At most
one prepared/dispatched/outcome-unknown attempt exists per operation, step, and
mode. Provider calls and domain fence checks happen outside this provider.

Receipts are append-only and contain opaque evidence reference plus SHA-256
digest. The dormant first car rejects every non-null summary until a trusted
redactor is wired, so it never contains environment values, secrets, stdout,
message bodies, or paths. Replaying the same receipt ID with
the same canonical identity returns the stored result without another
mutation; a different identity is `idempotency_conflict`.

A response-lost or otherwise unknown outcome is recorded as
`outcome_unknown`, preserves the original execution ID, and moves the operation
to `needs_attention`. It is never converted to confirmed failure and cannot
create another attempt. A later executor may only reconcile using that original
execution ID and explicit provider evidence.

When an operation enters `needs_attention`, any sibling attempt that is only
`prepared` is known locally not to have crossed the provider boundary. The
Journal atomically records a deterministic `not_executed` receipt for that
execution ID and releases its active slot. Already dispatched siblings remain
eligible to append late receipts; their evidence cannot move the operation out
of `needs_attention`. Normal parallel successes keep the operation `running`
until the caller performs an explicit CAS transition to `succeeded`.

## Read projection

The successful G3 response has exact top-level keys `data` and `meta`.
`data` has exactly five fields:

```text
operation, preconditions, steps, attempts, receipts
```

Collections are deterministically ordered. The private idempotency key and
storage path are not projected. `meta.partial=false`, source is
`operation_journal`, and capabilities are:

```text
operations.read      = true
operations.execute   = false / operation_executor_not_wired
operations.retry     = false / operation_executor_not_wired
operations.reconcile = false / operation_executor_not_wired
```

Unknown operation IDs return `operation_not_found`. Missing, unsafe, drifted,
or unreadable stores return a stable `503` error envelope without paths, SQL,
payloads, keys, or exception text.
