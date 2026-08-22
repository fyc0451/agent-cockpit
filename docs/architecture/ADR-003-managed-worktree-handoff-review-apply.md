# ADR-003: Managed worktree handoff, review, and apply authority

- Status: accepted for the M3 vertical slice
- Baseline: `agent-cockpit-next@f1a8a35`
- Product authority: the 2026-08-22 final optimization plan
- Depends on: ADR-001

## Context

Agent Cockpit already has two partially overlapping execution paths:

- `workspace_work_store` owns durable Boss messages, WorkItems, writer claims,
  replies, and receipts. `workspace_execution_store` owns managed checkouts,
  runtime attachments, and fenced writer leases. The Codex MCP tools can edit
  only the managed checkout.
- legacy `tasks.py` can run a detached Codex worktree, preview a diff, and
  cherry-pick it into a clean source repository at an exact base SHA.

Neither path currently owns the complete product transition from a scoped
WorkItem through an immutable handoff and independent review to an auditable
apply. In particular, `constraints` is free text rather than an enforceable
file boundary, and `reply_complete` can mark work completed before any review
or source-repository apply.

M3 must close one managed Codex path without creating another Mission Board,
Task DAG, scheduler, or general-purpose orchestration system.

## Decision

### Authorities

The existing workspace stack is canonical for new isolated work:

| Fact | Authority |
| --- | --- |
| Boss intent, acceptance, `allowed_paths`, WorkItem state | `workspace-work.sqlite3` |
| writer identity, generation, managed checkout, attachment, fenced lease | `workspace-execution.sqlite3` |
| runtime operation intent and step execution | operation journal |
| base tree, changed files, immutable result commit | Git |
| Handoff, Review, and Apply receipt | `workspace-work.sqlite3` |

`tasks.py` remains a legacy compatibility implementation. Its safe diff and
cherry-pick checks may be reused as implementation guidance, but its task row,
status, preview hash, or worktree is not imported as workspace authority.

An **Attempt** is the existing tuple of WorkItem preparation, writer claim,
managed checkout, runtime attachment, and fenced writer lease. M3 does not add
a second generic attempts table. The WorkItem and preparation IDs plus identity
generation identify one attempt. The current one-preparation-per-WorkItem rule
means a failed or rejected M3 item is terminal; retry creates a new WorkItem.

### File scope

`allowed_paths` is a required, non-empty ordered JSON list on an isolated
WorkItem. Every entry is a repository-relative POSIX path:

- `path/to/file.py` allows that exact path;
- `path/to/directory/` allows descendants of that directory;
- absolute paths, backslashes, empty segments, `.` and `..` are rejected;
- duplicate or overlapping entries are normalized away by the server.

The scope becomes immutable once a writer claim is reserved. Both the Codex
`apply_patch` tool and Handoff publication validate every old and new diff path
against it. Apply validates the committed diff again. A failed check returns
`path_outside_allowed_scope` before source Git state is changed.

Free-text `constraints` remains explanatory and never grants file authority.
Shared-directory mode A is unchanged and does not acquire this field
implicitly.

### State machine

For the M3 isolated slice the delivery lifecycle transitions are:

```text
unassigned -> working -> review -> completed
                    \-> failed
                              ^
review --reject-------------> failed
```

- `unassigned -> working` occurs only after the managed checkout and fenced
  writer lease are active.
- `working -> review` publishes one immutable Handoff and closes writer write
  authority. A normal agent reply is evidence attached to the Handoff; it is
  not completion.
- `review -> completed` occurs only after an accepted Review and a successful
  or explicitly no-change Apply receipt.
- preparation, execution, Handoff publication, rejection, apply failure, and
  outcome-unknown states retain evidence. They never pretend completion.

The compatibility `work_items.status` remains `working` while the isolated
delivery is in `review`; the M3 read model exposes `delivery_status` and
`delivery_revision` as the authoritative lifecycle. Apply success maps both to
`completed`, while rejection or a known terminal failure maps both to `failed`.

There is one published Handoff and one terminal Review for this first vertical
slice. Later retry/revision support requires a new ADR rather than mutating an
accepted packet.

### Handoff

The service, not the worker, derives and persists the Handoff from the managed
checkout. It stages the checkout with hooks disabled, computes the binary diff,
validates `allowed_paths`, and records:

- WorkItem, writer claim, checkout, identity, and generation;
- exact `base_sha`, immutable `head_sha`, and canonical `diff_digest`;
- sorted changed paths;
- worker summary and structured test evidence supplied by the worker;
- creation time and Handoff revision.

For a non-empty diff the service creates one commit in the managed checkout
using a fixed Cockpit identity and disabled hooks. The `head_sha` is therefore
reviewable and cannot change underneath the Review. A no-change Handoff has
`head_sha=base_sha` and an explicit empty diff digest.

After publication the writer lease is revoked. A mismatch between the checkout
HEAD and its recorded base, an untracked file that cannot be represented in the
staged diff, or an out-of-scope path rejects publication and keeps the checkout
for diagnosis.

### Review

A Review is an immutable accept or reject decision over exactly
`(handoff_id, handoff_revision, head_sha, diff_digest)`. The reviewer identity
and generation must differ from the Handoff author identity. Reviewing a stale
packet, self-reviewing, or replacing a terminal Review is rejected.

Review records a decision, summary, and optional test evidence. Acceptance is
permission to request apply, not permission for the server to apply
automatically. Rejection moves the first-slice WorkItem to `failed` and keeps
the checkout, Handoff, and evidence.

### Apply

Only an explicit Boss/lead apply command may start Apply. Agent completion,
review acceptance, dispatch, or a previous push authorization is never apply
authorization.

Apply is serialized per source repository and revalidates inside that lock:

1. the WorkItem is in `review` with an accepted exact Review;
2. the managed checkout still resolves to the recorded Handoff commit and the
   recomputed binary diff digest and changed paths match;
3. the source repository is clean and its `HEAD` exactly equals `base_sha`;
4. every changed path remains inside immutable `allowed_paths`;
5. the reviewed `head_sha` is cherry-picked with hooks disabled.

Success records the source before/after SHAs, Handoff and Review IDs, commit,
diff digest, changed paths, and operation receipt, then marks the WorkItem
`completed`. A no-change Handoff records a no-change Apply without moving
source HEAD. Cleanup of the managed checkout happens only after that durable
receipt commits.

If validation or cherry-pick fails, the service aborts any in-progress
cherry-pick, records a stable failure reason, leaves the WorkItem non-completed,
and preserves the checkout and immutable packet. If abort or persistence has an
unknown outcome, Apply becomes `outcome_unknown`; no retry is allowed until a
reconciliation command proves source HEAD and operation state. If source HEAD
already has one clean child of `base_sha` whose diff digest and changed paths
exactly match the accepted Handoff, retry may reconcile that result and append
the missing success receipt without applying it again.

### Public and worker boundaries

- The existing create/read model remains stable for shared work. The isolated
  M3 create contract adds explicit `allowed_paths`; it does not infer scope from
  prompts or `constraints`.
- Codex receives WorkItem text, acceptance, constraints, and `allowed_paths`,
  but never the source repository path. Its only write capability remains the
  fenced managed-checkout tool.
- Handoff publication is a distinct worker command. Existing
  `reply_complete` remains available only for non-review legacy flows and must
  not be used by the isolated M3 dispatcher.
- Review and apply are service commands. The browser or caller supplies opaque
  IDs, expected revisions, and idempotency keys, never filesystem paths or Git
  commit claims.

## Rejected alternatives

- **Make legacy `tasks.py` canonical.** It lacks workspace identity,
  generation-fenced claims, append-only evidence, and an independent Review.
- **Treat `reply_complete` as reviewed completion.** It conflates worker report,
  review, source mutation, and product completion.
- **Put `allowed_paths` in prompt text.** Prompt compliance is not a write
  boundary and cannot protect apply.
- **Auto-apply after agent success or Review acceptance.** This removes the
  explicit lead authority chosen for worktree mode B.
- **Build Task UI or scheduler first.** UI would freeze an authority model that
  is not yet enforced and is outside the M3 slice.

## Verification for the first vertical slice

The implementation is accepted only when tests prove:

1. an in-scope Codex patch changes only its managed checkout;
2. an out-of-scope old or new path is blocked at patch, Handoff, and Apply;
3. Handoff binds exact base/head/diff/files/test evidence and closes writer
   authority;
4. self-review and stale Review are rejected;
5. apply requires explicit accepted Review, a clean exact-base source, and an
   unchanged packet;
6. successful apply is traceable and failed/unknown apply preserves evidence;
7. existing HTTP/SSE/chat read models and shared-directory mode remain
   unchanged.

## Consequences

M3 adds a narrow review/apply domain to the workspace authorities and a schema
migration, but no second Task product. The isolated flow has stronger safety at
the cost of intentionally terminal failures in its first version. Multi-attempt
retry, automatic scheduling, UI boards, arbitrary providers, and parallel
review policies remain deferred.
