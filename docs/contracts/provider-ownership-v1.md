# Provider Ownership v1

## Purpose

`DELIVERY-003-wip4-gate` may raise active writer capacity from three to four
only after its immutable Git candidate proves separate ownership for Operation,
Runtime Provider, Event, and Memory. This contract plans ownership boundaries;
it does not claim that any provider is implemented.

The machine evidence is `.delivery/provider-ownership-v1.json` from the gate
car's `fixed_sha` Git tree. The validator never reads a working-tree sidecar.

## Exact Schema

The sidecar has exactly these top-level fields:

```json
{
  "schema_version": 1,
  "gate_id": "DELIVERY-003-wip4-gate",
  "transition": {"from": 3, "to": 4},
  "global_hotspots": [
    "agent_cockpit/runtime_paths.py",
    "agent_cockpit/server.py",
    "agent_cockpit/store_schema.py",
    "server.py"
  ],
  "partitions": []
}
```

`partitions` contains exactly four objects, in this order:

```text
operation        -> OPERATION-001-journal
runtime_provider -> RUNTIME-002-provider
event            -> EVENT-001-journal
memory           -> MEMORY-001-store
```

Each partition has exactly:

```json
{
  "id": "operation",
  "car_id": "OPERATION-001-journal",
  "scopes": ["sorted repo-relative paths"],
  "store_migration_scope": "one owned scope",
  "entrypoint_scope": "one owned scope"
}
```

Scopes are non-empty, sorted, unique, repo-relative paths. A partition cannot
contain duplicate, equivalent, ancestor, or descendant scopes. Its migration
and entrypoint scopes must be within its declared scopes.

## Global Hotspots

`global_hotspots` is mandatory and must exactly equal the four paths in the
schema example. No provider partition may overlap one of these paths. Provider
entrypoints are separate API modules; shared server wiring and shared schema or
runtime path registries remain serialized work outside provider writer cars.

## Immutable Evidence

For DELIVERY-003 in `review` or a later SHA-bearing status, validation requires:

1. exact, distinct `base_sha` and `fixed_sha` commits;
2. `base_sha` is an ancestor of `fixed_sha`;
3. the sidecar exists in the `fixed_sha` tree;
4. the sidecar is new or has a different blob from the base tree;
5. the fixed-tree Delivery plan contains all four planned cars;
6. every car depends on DELIVERY-003 and its scope exactly equals its partition;
7. the current Delivery plan preserves those IDs, dependencies, and scopes while
   normal car status transitions may proceed;
8. provider scopes, migration scopes, and entrypoint scopes do not overlap each
   other, global hotspots, or another active/runnable writer car.

Historical accepted cars do not permanently reserve broad scopes. Only the
four provider cars, active/runnable writer cars, and global hotspots participate
in this ownership proof.

Review validates evidence but keeps effective writer WIP at three. Only an
`accepted` or `user_accepted` DELIVERY-003 with valid evidence raises it to
four. A rejected, reverted, missing, malformed, stale, or mismatched proof
fails closed to three.

## Stable Errors

| Code | Meaning |
| --- | --- |
| `provider_ownership_evidence_required` | Missing/no-op/non-descendant/uncommitted/unchanged immutable evidence |
| `invalid_provider_ownership_evidence` | Invalid JSON, schema, transition, partition set, hotspot set, or path shape |
| `provider_ownership_car_mismatch` | Fixed/current Delivery car binding differs from the sidecar |
| `provider_ownership_overlap` | Provider, migration, entrypoint, hotspot, or runnable-writer ownership overlaps |
