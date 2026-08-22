"""M3 managed-checkout Handoff/Review/apply orchestration."""
from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import workspace_delivery_store as delivery_mod
from . import workspace_execution_store as execution_mod
from . import workspace_work_store as work_mod


class WorkspaceDeliveryServiceError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise WorkspaceDeliveryServiceError(code)


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _source_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def _run(path: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args], input=input_bytes,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        _fail("git_unavailable")
    if result.returncode != 0:
        _fail("git_command_failed")
    return result.stdout


def _head(path: Path) -> str:
    return _run(path, "rev-parse", "HEAD").decode().strip()


def _parent(path: Path) -> str | None:
    try:
        return _run(path, "rev-parse", "HEAD^").decode().strip()
    except WorkspaceDeliveryServiceError:
        return None


def _status(path: Path) -> str:
    return _run(path, "status", "--porcelain").decode(errors="replace")


def _diff(path: Path, base: str, head: str | None = None) -> bytes:
    if head is None:
        return _run(
            path, "diff", "--cached", "--no-renames", "--binary", "--full-index",
        )
    return _run(path, "diff", "--no-renames", "--binary", "--full-index", base, head)


def _paths(path: Path, base: str, head: str | None = None) -> tuple[str, ...]:
    args = ["diff", "--no-renames", "--name-only", "-z"]
    if head is None:
        args.insert(1, "--cached")
    else:
        args.extend((base, head))
    raw = _run(path, *args)
    try:
        values = tuple(sorted(item.decode("utf-8") for item in raw.split(b"\0") if item))
    except UnicodeDecodeError:
        _fail("invalid_changed_path")
    for item in values:
        if not work_mod.path_is_valid(item):
            _fail("invalid_changed_path")
    return values


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _evidence(value: object) -> str:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()
    return _digest(raw)


@dataclass(frozen=True)
class WorkspaceDeliveryService:
    execution: execution_mod.WorkspaceExecutionStore
    delivery: delivery_mod.WorkspaceDeliveryStore
    source_path_provider: Callable[[str, str], Path]

    def publish_handoff(
        self, *, project_id: str, workspace_id: str, work_item_id: str,
        attachment_id: str, identity_id: str, generation: int,
        expected_claim_revision: int, expected_work_revision: int,
        expected_lease_revision: int, summary: object, test_evidence: object,
        idempotency_key: str,
    ) -> dict[str, object]:
        prep = self.execution.find_preparation_for_attachment(
            attachment_id=attachment_id,
        )
        if (
            prep is None or prep.checkout is None or prep.lease is None
            or prep.attachment is None or prep.work_item_id != work_item_id
        ):
            _fail("preparation_not_found")
        if (
            prep.identity.identity_id != identity_id
            or prep.lease.generation != generation
            or prep.attachment.generation != generation
        ):
            _fail("stale_generation")
        try:
            normalized_summary = work_mod.body_text(summary)
            normalized_evidence = json.loads(work_mod._evidence(test_evidence))
        except work_mod.WorkspaceWorkError as exc:
            _fail(exc.code)
        replay = self.delivery.replay_command(
            project_id=project_id, workspace_id=workspace_id,
            scope=work_mod.HANDOFF_SCOPE,
            idempotency_key=idempotency_key + ":store",
        )
        if replay is not None:
            handoff = replay.get("handoff")
            if (
                isinstance(handoff, dict)
                and handoff.get("work_item_id") == work_item_id
                and handoff.get("claim_id") == prep.lease.claim_id
                and handoff.get("author_identity_id") == identity_id
                and handoff.get("author_generation") == generation
                and handoff.get("summary") == normalized_summary
                and handoff.get("test_evidence") == normalized_evidence
                and replay.get("request_claim_revision")
                == expected_claim_revision
                and replay.get("request_lease_revision")
                == expected_lease_revision
            ):
                return {**replay, "lease_status": "closed"}
            _fail("idempotency_conflict")
        if prep.lease.status != "active" or prep.lease.claim_id is None:
            _fail("lease_not_active")
        if prep.lease.revision != expected_lease_revision:
            _fail("stale_revision")
        checkout = Path(self.execution.checkout_internal_path(
            project_id=project_id, workspace_id=workspace_id,
            work_item_id=work_item_id,
        ))
        base_sha = prep.checkout.source_head
        if _head(checkout) != base_sha:
            _fail("checkout_changed")
        _run(checkout, "add", "-A")
        diff = _diff(checkout, base_sha)
        changed_paths = _paths(checkout, base_sha)
        allowed = self.delivery_allowed_paths(project_id, workspace_id, work_item_id)
        if any(not work_mod.path_is_allowed(path, allowed) for path in changed_paths):
            _fail("path_outside_allowed_scope")
        if _status(checkout) and not diff:
            _fail("checkout_changed")
        diff_digest = _digest(diff)
        if diff:
            _run(
                checkout, "-c", "core.hooksPath=/dev/null",
                "-c", "commit.gpgSign=false",
                "-c", "user.name=Agent Cockpit",
                "-c", "user.email=agent-cockpit@localhost",
                "commit", "-m", f"cockpit handoff: {work_item_id}",
            )
            head_sha = _head(checkout)
            committed_diff = _diff(checkout, base_sha, head_sha)
            committed_paths = _paths(checkout, base_sha, head_sha)
            if _digest(committed_diff) != diff_digest or committed_paths != changed_paths:
                _run(checkout, "reset", "--soft", base_sha)
                _fail("handoff_digest_mismatch")
        else:
            head_sha = base_sha
        try:
            published = self.delivery.publish_handoff(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id, claim_id=prep.lease.claim_id,
                author_identity_id=identity_id, author_generation=generation,
                checkout_id=prep.checkout.checkout_id, base_sha=base_sha,
                head_sha=head_sha, diff_digest=diff_digest,
                changed_paths=changed_paths, summary=summary,
                test_evidence=test_evidence,
                expected_claim_revision=expected_claim_revision,
                expected_work_revision=expected_work_revision,
                expected_lease_revision=expected_lease_revision,
                idempotency_key=idempotency_key + ":store",
            )
        except delivery_mod.WorkspaceDeliveryError as exc:
            replay = self.delivery.replay_command(
                project_id=project_id, workspace_id=workspace_id,
                scope=work_mod.HANDOFF_SCOPE,
                idempotency_key=idempotency_key + ":store",
            )
            if replay is not None:
                return {**replay, "lease_status": "reconcile_required"}
            if diff:
                try:
                    _run(checkout, "reset", "--soft", base_sha)
                except WorkspaceDeliveryServiceError:
                    _fail("handoff_outcome_unknown")
            _fail(exc.code)
        try:
            begun = self.execution.begin_reply(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id,
                expected_preparation_revision=prep.revision,
                expected_lease_revision=expected_lease_revision,
                attachment_id=attachment_id, identity_id=identity_id,
                generation=generation, claim_id=prep.lease.claim_id,
                idempotency_key=idempotency_key + ":revoke-begin",
            )
            finished = self.execution.finish_reply(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id,
                expected_preparation_revision=prep.revision,
                expected_lease_revision=int(begun["lease"]["revision"]),
                attachment_id=attachment_id, identity_id=identity_id,
                generation=generation, claim_id=prep.lease.claim_id,
                idempotency_key=idempotency_key + ":revoke-finish",
            )
        except execution_mod.WorkspaceExecutionError:
            # The workspace claim is already closed, so the write gate is shut.
            # Preserve the Handoff and expose the reconciliation requirement.
            return {**published, "lease_status": "reconcile_required"}
        return {**published, "lease": finished["lease"]}

    def delivery_allowed_paths(
        self, project_id: str, workspace_id: str, work_item_id: str,
    ) -> tuple[str, ...]:
        packet = self.delivery.get_packet(
            project_id=project_id, workspace_id=workspace_id,
            work_item_id=work_item_id,
        )
        if packet is None:
            _fail("review_authority_unavailable")
        try:
            return work_mod.normalize_allowed_paths(packet["allowed_paths"])
        except work_mod.WorkspaceWorkError as exc:
            _fail(exc.code)
        raise AssertionError("unreachable")

    def apply(
        self, *, project_id: str, workspace_id: str, work_item_id: str,
        expected_delivery_revision: int, idempotency_key: str,
    ) -> dict[str, object]:
        if type(expected_delivery_revision) is not int or expected_delivery_revision < 1:
            _fail("invalid_argument")
        for suffix in (":success", ":failed", ":outcome-unknown"):
            replay = self.delivery.replay_command(
                project_id=project_id, workspace_id=workspace_id,
                scope=work_mod.APPLY_SCOPE,
                idempotency_key=idempotency_key + suffix,
            )
            if replay is not None:
                if replay.get("work_item_id") != work_item_id:
                    _fail("idempotency_conflict")
                if replay.get("expected_delivery_revision") != expected_delivery_revision:
                    _fail("idempotency_conflict")
                if replay.get("outcome") == "failed":
                    reason = replay.get("reason")
                    _fail(reason if isinstance(reason, str) else "git_command_failed")
                if replay.get("outcome") in {"succeeded", "no_change"}:
                    return replay
                if replay.get("outcome") == "outcome_unknown":
                    _fail("apply_outcome_unknown")
                _fail("idempotency_conflict")
        packet = self.delivery.get_packet(
            project_id=project_id, workspace_id=workspace_id,
            work_item_id=work_item_id,
        )
        if packet is None or packet["handoff"] is None or packet["review"] is None:
            _fail("review_not_found")
        handoff = packet["handoff"]
        review = packet["review"]
        if review["decision"] != "accept":
            _fail("review_not_accepted")
        if packet["delivery_revision"] != expected_delivery_revision:
            _fail("stale_revision")
        if (
            review["handoff_revision"] != handoff["revision"]
            or review["head_sha"] != handoff["head_sha"]
            or review["diff_digest"] != handoff["diff_digest"]
        ):
            _fail("stale_handoff")
        checkout = Path(self.execution.checkout_internal_path(
            project_id=project_id, workspace_id=workspace_id,
            work_item_id=work_item_id,
        ))
        source = Path(self.source_path_provider(project_id, workspace_id))
        allowed = work_mod.normalize_allowed_paths(packet["allowed_paths"])
        with _source_lock(source):
            source_before = _head(source)
            cherry_pick_attempted = False
            try:
                if _status(source):
                    _fail("source_dirty")
                if source_before != handoff["base_sha"]:
                    parent = _parent(source)
                    recovered_diff = _diff(
                        source, handoff["base_sha"], source_before,
                    )
                    recovered_paths = _paths(
                        source, handoff["base_sha"], source_before,
                    )
                    recovered = (
                        parent == handoff["base_sha"]
                        and _digest(recovered_diff) == handoff["diff_digest"]
                        and list(recovered_paths) == handoff["changed_paths"]
                        and all(
                            work_mod.path_is_allowed(path, allowed)
                            for path in recovered_paths
                        )
                    )
                    if not recovered:
                        _fail("source_changed")
                    try:
                        return self.delivery.record_apply(
                            project_id=project_id, workspace_id=workspace_id,
                            work_item_id=work_item_id,
                            handoff_id=handoff["handoff_id"],
                            review_id=review["review_id"],
                            expected_delivery_revision=expected_delivery_revision,
                            outcome="succeeded",
                            source_before_sha=handoff["base_sha"],
                            source_after_sha=source_before,
                            applied_commit_sha=source_before, reason=None,
                            evidence_digest=_evidence({
                                "diff_digest": handoff["diff_digest"],
                                "handoff_id": handoff["handoff_id"],
                                "reconciled": True,
                                "review_id": review["review_id"],
                                "source_after": source_before,
                                "source_before": handoff["base_sha"],
                            }), idempotency_key=idempotency_key + ":success",
                        )
                    except delivery_mod.WorkspaceDeliveryError:
                        _fail("apply_outcome_unknown")
                if _head(checkout) != handoff["head_sha"]:
                    _fail("stale_handoff")
                diff = _diff(checkout, handoff["base_sha"], handoff["head_sha"])
                changed_paths = _paths(
                    checkout, handoff["base_sha"], handoff["head_sha"],
                )
                if (
                    _digest(diff) != handoff["diff_digest"]
                    or list(changed_paths) != handoff["changed_paths"]
                ):
                    _fail("stale_handoff")
                if any(
                    not work_mod.path_is_allowed(path, allowed)
                    for path in changed_paths
                ):
                    _fail("path_outside_allowed_scope")
                if handoff["head_sha"] == handoff["base_sha"]:
                    outcome = "no_change"
                    source_after = source_before
                    applied_commit = None
                else:
                    cherry_pick_attempted = True
                    _run(
                        source, "-c", "core.hooksPath=/dev/null",
                        "cherry-pick", "--no-gpg-sign", handoff["head_sha"],
                    )
                    outcome = "succeeded"
                    source_after = _head(source)
                    applied_commit = source_after
            except WorkspaceDeliveryServiceError as exc:
                if cherry_pick_attempted:
                    try:
                        aborted = subprocess.run(
                            ["git", "-C", str(source), "cherry-pick", "--abort"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            check=False, timeout=30,
                        )
                        abort_safe = (
                            aborted.returncode == 0
                            and _head(source) == source_before
                            and not _status(source)
                        )
                    except (OSError, subprocess.SubprocessError,
                            WorkspaceDeliveryServiceError):
                        abort_safe = False
                    if not abort_safe:
                        try:
                            current_head = _head(source)
                        except WorkspaceDeliveryServiceError:
                            current_head = None
                        try:
                            self.delivery.record_apply(
                                project_id=project_id, workspace_id=workspace_id,
                                work_item_id=work_item_id,
                                handoff_id=handoff["handoff_id"],
                                review_id=review["review_id"],
                                expected_delivery_revision=expected_delivery_revision,
                                outcome="outcome_unknown",
                                source_before_sha=source_before,
                                source_after_sha=current_head,
                                applied_commit_sha=None,
                                reason="cherry_pick_abort_failed",
                                evidence_digest=_evidence({
                                    "handoff_id": handoff["handoff_id"],
                                    "reason": "cherry_pick_abort_failed",
                                    "source_after": current_head,
                                    "source_before": source_before,
                                }), idempotency_key=(
                                    idempotency_key + ":outcome-unknown"
                                ),
                            )
                        except delivery_mod.WorkspaceDeliveryError:
                            pass
                        _fail("apply_outcome_unknown")
                try:
                    self.delivery.record_apply(
                        project_id=project_id, workspace_id=workspace_id,
                        work_item_id=work_item_id,
                        handoff_id=handoff["handoff_id"],
                        review_id=review["review_id"],
                        expected_delivery_revision=expected_delivery_revision,
                        outcome="failed", source_before_sha=source_before,
                        source_after_sha=_head(source), applied_commit_sha=None,
                        reason=exc.code, evidence_digest=_evidence({
                            "handoff_id": handoff["handoff_id"],
                            "reason": exc.code, "source_before": source_before,
                        }), idempotency_key=idempotency_key + ":failed",
                    )
                except delivery_mod.WorkspaceDeliveryError:
                    _fail("apply_outcome_unknown")
                raise
            try:
                return self.delivery.record_apply(
                    project_id=project_id, workspace_id=workspace_id,
                    work_item_id=work_item_id,
                    handoff_id=handoff["handoff_id"],
                    review_id=review["review_id"],
                    expected_delivery_revision=expected_delivery_revision,
                    outcome=outcome, source_before_sha=source_before,
                    source_after_sha=source_after,
                    applied_commit_sha=applied_commit, reason=None,
                    evidence_digest=_evidence({
                        "changed_paths": changed_paths,
                        "diff_digest": handoff["diff_digest"],
                        "handoff_id": handoff["handoff_id"],
                        "review_id": review["review_id"],
                        "source_after": source_after,
                        "source_before": source_before,
                    }), idempotency_key=idempotency_key + ":success",
                )
            except delivery_mod.WorkspaceDeliveryError:
                _fail("apply_outcome_unknown")
        raise AssertionError("unreachable")
