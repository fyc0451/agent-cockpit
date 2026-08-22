"""M3 Handoff/Review/Apply facts stored in workspace-work.sqlite3."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import workspace_work_store as work_mod


class WorkspaceDeliveryError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise WorkspaceDeliveryError(code)


def _map(exc: BaseException) -> None:
    code = getattr(exc, "code", None)
    _fail(code if isinstance(code, str) else "store_write_failed")


def _json(value: str) -> object:
    try:
        return json.loads(value)
    except ValueError:
        _fail("store_corrupt")
    raise AssertionError("unreachable")


def _handoff_public(row: sqlite3.Row) -> dict[str, object]:
    return {
        "handoff_id": row["handoff_id"],
        "work_item_id": row["work_item_id"],
        "claim_id": row["claim_id"],
        "author_identity_id": row["author_identity_id"],
        "author_generation": int(row["author_generation"]),
        "checkout_id": row["checkout_id"],
        "base_sha": row["base_sha"],
        "head_sha": row["head_sha"],
        "diff_digest": row["diff_digest"],
        "changed_paths": _json(row["changed_paths_json"]),
        "summary": row["summary"],
        "test_evidence": _json(row["test_evidence_json"]),
        "revision": int(row["revision"]),
        "created_at": row["created_at"],
    }


def _review_public(row: sqlite3.Row) -> dict[str, object]:
    return {
        "review_id": row["review_id"],
        "handoff_id": row["handoff_id"],
        "reviewer_identity_id": row["reviewer_identity_id"],
        "reviewer_generation": int(row["reviewer_generation"]),
        "handoff_revision": int(row["handoff_revision"]),
        "head_sha": row["head_sha"],
        "diff_digest": row["diff_digest"],
        "decision": row["decision"],
        "summary": row["summary"],
        "test_evidence": _json(row["test_evidence_json"]),
        "created_at": row["created_at"],
    }


@dataclass(frozen=True)
class WorkspaceDeliveryStore:
    path: Path

    @classmethod
    def from_work_store(
        cls, store: work_mod.WorkspaceWorkStore,
    ) -> "WorkspaceDeliveryStore":
        return cls(store.path)

    def replay_command(
        self, *, project_id: str, workspace_id: str, scope: str,
        idempotency_key: str,
    ) -> dict[str, object] | None:
        project_id = work_mod._opaque(project_id, "prj_")
        workspace_id = work_mod._opaque(workspace_id, "ws_")
        key = work_mod._idempotency_key(idempotency_key)
        connection = work_mod._connect(self.path, write=False)
        try:
            work_mod._require_current_schema(connection)
            row = connection.execute(
                "SELECT response_json FROM idempotency_records "
                "WHERE project_id=? AND workspace_id=? AND command_scope=? "
                "AND idempotency_key=?",
                (project_id, workspace_id, scope, key),
            ).fetchone()
            if row is None:
                return None
            value = _json(row["response_json"])
            if not isinstance(value, dict):
                _fail("store_corrupt")
            return value
        except work_mod.WorkspaceWorkError as exc:
            _map(exc)
        except sqlite3.Error:
            _fail("store_read_failed")
        finally:
            connection.close()
        raise AssertionError("unreachable")

    def publish_handoff(
        self, *, project_id: str, workspace_id: str, work_item_id: str,
        claim_id: object, author_identity_id: object, author_generation: object,
        checkout_id: object, base_sha: object, head_sha: object,
        diff_digest: object, changed_paths: object, summary: object,
        test_evidence: object, expected_claim_revision: object,
        expected_work_revision: object, expected_lease_revision: object,
        idempotency_key: object,
    ) -> dict[str, object]:
        project_id = work_mod._opaque(project_id, "prj_")
        workspace_id = work_mod._opaque(workspace_id, "ws_")
        work_item_id = work_mod._opaque(work_item_id, "wrk_")
        claim_id = work_mod._opaque(claim_id, "clm_")
        author_identity_id = work_mod._opaque(author_identity_id, "idn_")
        author_generation = work_mod._generation(author_generation)
        checkout_id = work_mod._opaque(checkout_id, "chk_")
        base_sha = work_mod._git_sha(base_sha)
        head_sha = work_mod._git_sha(head_sha)
        diff_digest = work_mod._sha256(diff_digest)
        paths = work_mod._changed_paths(changed_paths)
        summary = work_mod.body_text(summary)
        evidence_json = work_mod._evidence(test_evidence)
        expected_claim_revision = work_mod._revision(expected_claim_revision)
        expected_work_revision = work_mod._revision(expected_work_revision)
        expected_lease_revision = work_mod._revision(expected_lease_revision)
        key = work_mod._idempotency_key(idempotency_key)
        request = {
            "author_generation": author_generation,
            "author_identity_id": author_identity_id,
            "base_sha": base_sha,
            "changed_paths": list(paths),
            "checkout_id": checkout_id,
            "claim_id": claim_id,
            "diff_digest": diff_digest,
            "expected_claim_revision": expected_claim_revision,
            "expected_lease_revision": expected_lease_revision,
            "expected_work_revision": expected_work_revision,
            "head_sha": head_sha,
            "summary": summary,
            "test_evidence": _json(evidence_json),
            "work_item_id": work_item_id,
        }
        digest = work_mod._digest(request)

        def operate(connection: sqlite3.Connection) -> dict[str, object]:
            replay = work_mod._idempotency_row(
                connection, project_id=project_id, workspace_id=workspace_id,
                scope=work_mod.HANDOFF_SCOPE, key=key, digest=digest,
            )
            if replay is not None:
                return replay
            work = work_mod._load_work(
                connection, project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id,
            )
            if work is None:
                _fail("work_item_not_found")
            if work["allowed_paths_json"] is None:
                _fail("review_authority_unavailable")
            if (
                work["status"] != "working"
                or work["delivery_status"] != "working"
            ):
                _fail("handoff_conflict")
            if int(work["work_revision"]) != expected_work_revision:
                _fail("stale_revision")
            claim = connection.execute(
                "SELECT * FROM work_item_claims WHERE claim_id=? AND work_item_id=?",
                (claim_id, work_item_id),
            ).fetchone()
            if claim is None:
                _fail("work_item_not_found")
            if (
                claim["state"] != "active"
                or claim["identity_id"] != author_identity_id
            ):
                _fail("claim_not_active")
            if int(claim["generation"]) != author_generation:
                _fail("stale_generation")
            if int(claim["revision"]) != expected_claim_revision:
                _fail("stale_revision")
            allowed = work_mod.normalize_allowed_paths(
                _json(work["allowed_paths_json"])
            )
            if any(not work_mod.path_is_allowed(path, allowed) for path in paths):
                _fail("path_outside_allowed_scope")
            now = work_mod._now()
            handoff_id = work_mod._new_id("hnd_")
            connection.execute(
                "INSERT INTO work_item_handoffs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    handoff_id, work_item_id, claim_id, author_identity_id,
                    author_generation, checkout_id, base_sha, head_sha,
                    diff_digest, work_mod._canonical(list(paths)), summary,
                    evidence_json, 1, now,
                ),
            )
            ordinal = int(connection.execute(
                "SELECT MAX(ordinal) FROM messages WHERE thread_id=?",
                (work["thread_id"],),
            ).fetchone()[0]) + 1
            message_id = work_mod._new_id("msg_")
            connection.execute(
                "INSERT INTO messages (message_id,thread_id,ordinal,message_kind,"
                "author_kind,author_ref,author_generation,reply_to_message_id,body,"
                "created_at) VALUES (?,?,?,'reply','agent',?,?,?,?,?)",
                (
                    message_id, work["thread_id"], ordinal, author_identity_id,
                    author_generation, work["message_id"], summary, now,
                ),
            )
            connection.execute(
                "UPDATE message_threads SET revision=revision+1 WHERE thread_id=?",
                (work["thread_id"],),
            )
            connection.execute(
                "UPDATE work_item_claims SET state='closed',revision=revision+1,"
                "updated_at=? WHERE claim_id=?", (now, claim_id),
            )
            connection.execute(
                "UPDATE work_items SET revision=revision+1,updated_at=? "
                "WHERE work_item_id=?", (now, work_item_id),
            )
            connection.execute(
                "UPDATE work_item_deliveries SET status='review',"
                "revision=revision+1,updated_at=? WHERE work_item_id=?",
                (now, work_item_id),
            )
            row = connection.execute(
                "SELECT * FROM work_item_handoffs WHERE handoff_id=?",
                (handoff_id,),
            ).fetchone()
            assert row is not None
            payload = {
                "handoff": _handoff_public(row),
                "claim": {**work_mod._claim_public(claim), "state": "closed",
                          "revision": int(claim["revision"]) + 1},
                "delivery_status": "review",
                "delivery_revision": int(work["delivery_revision"]) + 1,
                "request_claim_revision": expected_claim_revision,
                "request_lease_revision": expected_lease_revision,
            }
            work_mod._remember(
                connection, project_id=project_id, workspace_id=workspace_id,
                scope=work_mod.HANDOFF_SCOPE, key=key, digest=digest,
                payload=payload,
            )
            return payload

        try:
            result = work_mod._write(self.path, operate)
        except work_mod.WorkspaceWorkError as exc:
            _map(exc)
        assert isinstance(result, dict)
        return result

    def review_handoff(
        self, *, project_id: str, workspace_id: str, work_item_id: str,
        handoff_id: object, reviewer_identity_id: object,
        reviewer_generation: object, expected_handoff_revision: object,
        expected_delivery_revision: object, head_sha: object,
        diff_digest: object, decision: object, summary: object,
        test_evidence: object, idempotency_key: object,
    ) -> dict[str, object]:
        project_id = work_mod._opaque(project_id, "prj_")
        workspace_id = work_mod._opaque(workspace_id, "ws_")
        work_item_id = work_mod._opaque(work_item_id, "wrk_")
        handoff_id = work_mod._opaque(handoff_id, "hnd_")
        reviewer_identity_id = work_mod._opaque(reviewer_identity_id, "idn_")
        reviewer_generation = work_mod._generation(reviewer_generation)
        expected_handoff_revision = work_mod._revision(expected_handoff_revision)
        expected_delivery_revision = work_mod._revision(expected_delivery_revision)
        head_sha = work_mod._git_sha(head_sha)
        diff_digest = work_mod._sha256(diff_digest)
        if decision not in {"accept", "reject"}:
            _fail("invalid_argument")
        assert isinstance(decision, str)
        summary = work_mod.body_text(summary)
        evidence_json = work_mod._evidence(test_evidence)
        key = work_mod._idempotency_key(idempotency_key)
        request = {
            "decision": decision, "diff_digest": diff_digest,
            "expected_delivery_revision": expected_delivery_revision,
            "expected_handoff_revision": expected_handoff_revision,
            "handoff_id": handoff_id, "head_sha": head_sha,
            "reviewer_generation": reviewer_generation,
            "reviewer_identity_id": reviewer_identity_id, "summary": summary,
            "test_evidence": _json(evidence_json), "work_item_id": work_item_id,
        }
        digest = work_mod._digest(request)

        def operate(connection: sqlite3.Connection) -> dict[str, object]:
            replay = work_mod._idempotency_row(
                connection, project_id=project_id, workspace_id=workspace_id,
                scope=work_mod.REVIEW_SCOPE, key=key, digest=digest,
            )
            if replay is not None:
                return replay
            work = work_mod._load_work(
                connection, project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id,
            )
            handoff = connection.execute(
                "SELECT * FROM work_item_handoffs WHERE handoff_id=? "
                "AND work_item_id=?", (handoff_id, work_item_id),
            ).fetchone()
            if work is None or handoff is None:
                _fail("handoff_not_found")
            if work["delivery_status"] != "review":
                _fail("review_conflict")
            if int(work["delivery_revision"]) != expected_delivery_revision:
                _fail("stale_revision")
            if int(handoff["revision"]) != expected_handoff_revision:
                _fail("stale_revision")
            if reviewer_identity_id == handoff["author_identity_id"]:
                _fail("self_review_forbidden")
            if head_sha != handoff["head_sha"] or diff_digest != handoff["diff_digest"]:
                _fail("stale_handoff")
            now = work_mod._now()
            review_id = work_mod._new_id("rvw_")
            connection.execute(
                "INSERT INTO work_item_reviews VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    review_id, handoff_id, reviewer_identity_id,
                    reviewer_generation, expected_handoff_revision, head_sha,
                    diff_digest, decision, summary, evidence_json, now,
                ),
            )
            # Accepted review authorizes explicit apply; only its receipt completes.
            next_status = "review" if decision == "accept" else "failed"
            connection.execute(
                "UPDATE work_item_deliveries SET status=?,revision=revision+1,"
                "updated_at=? WHERE work_item_id=?",
                (next_status, now, work_item_id),
            )
            if decision == "reject":
                connection.execute(
                    "UPDATE work_items SET status='failed',revision=revision+1,"
                    "updated_at=? WHERE work_item_id=?", (now, work_item_id),
                )
            row = connection.execute(
                "SELECT * FROM work_item_reviews WHERE review_id=?", (review_id,),
            ).fetchone()
            assert row is not None
            payload = {
                "review": _review_public(row), "delivery_status": next_status,
                "delivery_revision": expected_delivery_revision + 1,
            }
            work_mod._remember(
                connection, project_id=project_id, workspace_id=workspace_id,
                scope=work_mod.REVIEW_SCOPE, key=key, digest=digest,
                payload=payload,
            )
            return payload

        try:
            result = work_mod._write(self.path, operate)
        except work_mod.WorkspaceWorkError as exc:
            _map(exc)
        assert isinstance(result, dict)
        return result

    def get_packet(
        self, *, project_id: str, workspace_id: str, work_item_id: str,
    ) -> dict[str, object] | None:
        project_id = work_mod._opaque(project_id, "prj_")
        workspace_id = work_mod._opaque(workspace_id, "ws_")
        work_item_id = work_mod._opaque(work_item_id, "wrk_")
        connection = work_mod._connect(self.path, write=False)
        try:
            work_mod._require_current_schema(connection)
            connection.execute("BEGIN")
            work = work_mod._load_work(
                connection, project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id,
            )
            if work is None or work["allowed_paths_json"] is None:
                return None
            handoff = connection.execute(
                "SELECT * FROM work_item_handoffs WHERE work_item_id=?",
                (work_item_id,),
            ).fetchone()
            review = None if handoff is None else connection.execute(
                "SELECT * FROM work_item_reviews WHERE handoff_id=?",
                (handoff["handoff_id"],),
            ).fetchone()
            applied = None if handoff is None else connection.execute(
                "SELECT * FROM work_item_apply_receipts WHERE handoff_id=?",
                (handoff["handoff_id"],),
            ).fetchone()
            return {
                "allowed_paths": _json(work["allowed_paths_json"]),
                "delivery_status": work["delivery_status"],
                "delivery_revision": int(work["delivery_revision"]),
                "handoff": None if handoff is None else _handoff_public(handoff),
                "review": None if review is None else _review_public(review),
                "apply": None if applied is None else {
                    key: applied[key] for key in applied.keys()
                },
            }
        except work_mod.WorkspaceWorkError as exc:
            _map(exc)
        except sqlite3.Error:
            _fail("store_read_failed")
        finally:
            connection.close()
        raise AssertionError("unreachable")

    def record_apply(
        self, *, project_id: str, workspace_id: str, work_item_id: str,
        handoff_id: object, review_id: object,
        expected_delivery_revision: object, outcome: object,
        source_before_sha: object, source_after_sha: object,
        applied_commit_sha: object, reason: object, evidence_digest: object,
        idempotency_key: object,
    ) -> dict[str, object]:
        project_id = work_mod._opaque(project_id, "prj_")
        workspace_id = work_mod._opaque(workspace_id, "ws_")
        work_item_id = work_mod._opaque(work_item_id, "wrk_")
        handoff_id = work_mod._opaque(handoff_id, "hnd_")
        review_id = work_mod._opaque(review_id, "rvw_")
        expected_delivery_revision = work_mod._revision(expected_delivery_revision)
        if outcome not in {"succeeded", "no_change", "failed", "outcome_unknown"}:
            _fail("invalid_argument")
        assert isinstance(outcome, str)
        source_before_sha = work_mod._git_sha(source_before_sha)
        source_after_sha = (
            None if source_after_sha is None else work_mod._git_sha(source_after_sha)
        )
        applied_commit_sha = (
            None if applied_commit_sha is None else work_mod._git_sha(applied_commit_sha)
        )
        reason = work_mod.note_text(reason)
        evidence_digest = work_mod._sha256(evidence_digest)
        key = work_mod._idempotency_key(idempotency_key)
        request = {
            "applied_commit_sha": applied_commit_sha,
            "evidence_digest": evidence_digest,
            "expected_delivery_revision": expected_delivery_revision,
            "handoff_id": handoff_id, "outcome": outcome, "reason": reason,
            "review_id": review_id, "source_after_sha": source_after_sha,
            "source_before_sha": source_before_sha, "work_item_id": work_item_id,
        }
        digest = work_mod._digest(request)

        def operate(connection: sqlite3.Connection) -> dict[str, object]:
            replay = work_mod._idempotency_row(
                connection, project_id=project_id, workspace_id=workspace_id,
                scope=work_mod.APPLY_SCOPE, key=key, digest=digest,
            )
            if replay is not None:
                return replay
            work = work_mod._load_work(
                connection, project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id,
            )
            review = connection.execute(
                "SELECT r.*,h.work_item_id FROM work_item_reviews r "
                "JOIN work_item_handoffs h ON h.handoff_id=r.handoff_id "
                "WHERE r.review_id=? AND r.handoff_id=?",
                (review_id, handoff_id),
            ).fetchone()
            if work is None or review is None or review["work_item_id"] != work_item_id:
                _fail("review_not_found")
            if review["decision"] != "accept":
                _fail("review_not_accepted")
            if (
                work["delivery_status"] != "review"
                or int(work["delivery_revision"]) != expected_delivery_revision
            ):
                _fail("stale_revision")
            now = work_mod._now()
            apply_id = work_mod._new_id("apl_")
            connection.execute(
                "INSERT INTO work_item_apply_receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    apply_id, handoff_id, review_id, outcome, source_before_sha,
                    source_after_sha, applied_commit_sha, reason, evidence_digest, now,
                ),
            )
            delivery_status = {
                "succeeded": "completed", "no_change": "completed",
                "failed": "failed", "outcome_unknown": "outcome_unknown",
            }[outcome]
            connection.execute(
                "UPDATE work_item_deliveries SET status=?,revision=revision+1,"
                "updated_at=? WHERE work_item_id=?",
                (delivery_status, now, work_item_id),
            )
            connection.execute(
                "UPDATE work_items SET status=?,revision=revision+1,updated_at=? "
                "WHERE work_item_id=?",
                (
                    "completed" if delivery_status == "completed" else "failed",
                    now, work_item_id,
                ),
            )
            payload = {
                "apply_id": apply_id, "work_item_id": work_item_id,
                "outcome": outcome,
                "reason": reason,
                "expected_delivery_revision": expected_delivery_revision,
                "delivery_status": delivery_status,
                "delivery_revision": expected_delivery_revision + 1,
                "source_before_sha": source_before_sha,
                "source_after_sha": source_after_sha,
                "applied_commit_sha": applied_commit_sha,
                "evidence_digest": evidence_digest,
            }
            work_mod._remember(
                connection, project_id=project_id, workspace_id=workspace_id,
                scope=work_mod.APPLY_SCOPE, key=key, digest=digest,
                payload=payload,
            )
            return payload

        try:
            result = work_mod._write(self.path, operate)
        except work_mod.WorkspaceWorkError as exc:
            _map(exc)
        assert isinstance(result, dict)
        return result
