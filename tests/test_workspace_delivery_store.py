from __future__ import annotations

import pytest

from agent_cockpit import workspace_delivery_store as delivery_mod
from agent_cockpit import workspace_work_store as work_mod


PROJECT = "prj_" + "a" * 32
WORKSPACE = "ws_" + "b" * 32
AUTHOR = "idn_" + "1" * 32
REVIEWER = "idn_" + "2" * 32
BASE = "a" * 40
HEAD = "b" * 40
DIGEST = "sha256:" + "c" * 64


def _working(tmp_path):
    work = work_mod.initialize(tmp_path / "workspace-work.sqlite3")
    created = work.create_work_item(
        project_id=PROJECT, workspace_id=WORKSPACE, body="change README",
        acceptance="tests pass", constraints="local only",
        allowed_paths=["README.md"], idempotency_key="create",
    )
    work_id = created.item.work_item["work_item_id"]
    reserved = work.reserve_claim(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        identity_id=AUTHOR, generation=1, expected_revision=1,
        idempotency_key="reserve",
    )
    activated = work.activate_claim(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        claim_id=reserved["claim"]["claim_id"], identity_id=AUTHOR,
        generation=1, expected_claim_revision=1, expected_work_revision=1,
        idempotency_key="activate",
    )
    return work, delivery_mod.WorkspaceDeliveryStore.from_work_store(work), activated


def test_handoff_review_apply_is_exact_and_traceable(tmp_path) -> None:
    work, delivery, activated = _working(tmp_path)
    work_id = activated["work_item"]["work_item_id"]
    handoff = delivery.publish_handoff(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        claim_id=activated["claim"]["claim_id"], author_identity_id=AUTHOR,
        author_generation=1, checkout_id="chk_" + "d" * 32,
        base_sha=BASE, head_sha=HEAD, diff_digest=DIGEST,
        changed_paths=["README.md"], summary="implemented and tested",
        test_evidence={"commands": ["pytest"], "passed": True},
        expected_claim_revision=activated["claim"]["revision"],
        expected_work_revision=activated["work_item"]["revision"],
        expected_lease_revision=1,
        idempotency_key="handoff",
    )
    assert handoff["delivery_status"] == "review"
    assert handoff["claim"]["state"] == "closed"
    with pytest.raises(delivery_mod.WorkspaceDeliveryError) as self_review:
        delivery.review_handoff(
            project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
            handoff_id=handoff["handoff"]["handoff_id"],
            reviewer_identity_id=AUTHOR, reviewer_generation=1,
            expected_handoff_revision=1,
            expected_delivery_revision=handoff["delivery_revision"],
            head_sha=HEAD, diff_digest=DIGEST, decision="accept",
            summary="looks good", test_evidence={}, idempotency_key="self",
        )
    assert self_review.value.code == "self_review_forbidden"
    reviewed = delivery.review_handoff(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        handoff_id=handoff["handoff"]["handoff_id"],
        reviewer_identity_id=REVIEWER, reviewer_generation=1,
        expected_handoff_revision=1,
        expected_delivery_revision=handoff["delivery_revision"],
        head_sha=HEAD, diff_digest=DIGEST, decision="accept",
        summary="verified", test_evidence={"reviewed": True},
        idempotency_key="review",
    )
    applied = delivery.record_apply(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        handoff_id=handoff["handoff"]["handoff_id"],
        review_id=reviewed["review"]["review_id"],
        expected_delivery_revision=reviewed["delivery_revision"],
        outcome="succeeded", source_before_sha=BASE, source_after_sha=HEAD,
        applied_commit_sha=HEAD, reason=None,
        evidence_digest="sha256:" + "e" * 64, idempotency_key="apply",
    )
    assert applied["delivery_status"] == "completed"
    packet = delivery.get_packet(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
    )
    assert packet is not None
    assert packet["handoff"]["changed_paths"] == ["README.md"]
    assert packet["review"]["reviewer_identity_id"] == REVIEWER
    assert packet["apply"]["outcome"] == "succeeded"
    detail = work.get_work_item_detail(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
    )
    assert detail is not None
    assert detail["work_item"]["status"] == "completed"
    work.close()


def test_handoff_blocks_out_of_scope_path_and_keeps_writer_active(tmp_path) -> None:
    work, delivery, activated = _working(tmp_path)
    with pytest.raises(delivery_mod.WorkspaceDeliveryError) as outside:
        delivery.publish_handoff(
            project_id=PROJECT, workspace_id=WORKSPACE,
            work_item_id=activated["work_item"]["work_item_id"],
            claim_id=activated["claim"]["claim_id"],
            author_identity_id=AUTHOR, author_generation=1,
            checkout_id="chk_" + "d" * 32, base_sha=BASE, head_sha=HEAD,
            diff_digest=DIGEST, changed_paths=["agent_cockpit/server.py"],
            summary="outside", test_evidence={},
            expected_claim_revision=activated["claim"]["revision"],
            expected_work_revision=activated["work_item"]["revision"],
            expected_lease_revision=1,
            idempotency_key="outside",
        )
    assert outside.value.code == "path_outside_allowed_scope"
    detail = work.get_work_item_detail(
        project_id=PROJECT, workspace_id=WORKSPACE,
        work_item_id=activated["work_item"]["work_item_id"],
    )
    assert detail is not None
    assert detail["claim"]["state"] == "active"
    assert detail["work_item"]["delivery_status"] == "working"
    work.close()
