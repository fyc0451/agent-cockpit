from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_cockpit import workspace_delivery_api as api


PROJECT = "prj_" + "a" * 32
WORKSPACE = "ws_" + "b" * 32
WORK = "wrk_" + "c" * 32
HANDOFF = "hnd_" + "d" * 32
REVIEWER = "idn_" + "e" * 32
HEAD = "a" * 40
DIGEST = "sha256:" + "b" * 64


class _Store:
    def __init__(self) -> None:
        self.reviewed = None

    def get_packet(self, **_kwargs):
        return {
            "allowed_paths": ["README.md"], "delivery_status": "review",
            "delivery_revision": 3, "handoff": None, "review": None,
            "apply": None,
        }

    def review_handoff(self, **kwargs):
        assert kwargs["handoff_id"] == HANDOFF
        assert kwargs["expected_handoff_revision"] == 1
        assert kwargs["expected_delivery_revision"] == 3
        assert kwargs["head_sha"] == HEAD
        assert kwargs["diff_digest"] == DIGEST
        self.reviewed = kwargs
        return {"review": {"decision": "accept"}, "delivery_revision": 4}


class _Service:
    def __init__(self) -> None:
        self.applied = None

    def apply(self, **kwargs):
        assert kwargs["expected_delivery_revision"] == 4
        self.applied = kwargs
        return {"outcome": "succeeded", "delivery_status": "completed"}


def test_review_and_apply_are_explicit_strict_commands() -> None:
    store = _Store()
    service = _Service()
    app = FastAPI()
    api.install(
        app, service_provider=lambda: service, store_provider=lambda: store,
        identity_provider=lambda _p, _w, identity: (
            SimpleNamespace(revision=1) if identity == REVIEWER else None
        ),
    )
    http = TestClient(app)
    base = f"/api/projects/{PROJECT}/workspaces/{WORKSPACE}/work-items/{WORK}"
    reviewed = http.post(
        base + "/reviews",
        json={
            "handoff_id": HANDOFF, "reviewer_identity_id": REVIEWER,
            "reviewer_generation": 1, "expected_handoff_revision": 1,
            "expected_delivery_revision": 3, "head_sha": HEAD,
            "diff_digest": DIGEST, "decision": "accept",
            "summary": "exact packet reviewed", "test_evidence": {},
        },
        headers={"Idempotency-Key": "review"},
    )
    assert reviewed.status_code == 200
    assert store.reviewed["handoff_id"] == HANDOFF
    applied = http.post(
        base + "/apply", json={"expected_delivery_revision": 4},
        headers={"Idempotency-Key": "apply"},
    )
    assert applied.status_code == 200
    assert service.applied == {
        "project_id": PROJECT, "workspace_id": WORKSPACE,
        "work_item_id": WORK, "expected_delivery_revision": 4,
        "idempotency_key": "apply",
    }
    packet = http.get(base + "/delivery")
    assert packet.status_code == 200
    assert packet.json()["data"]["delivery_status"] == "review"


def test_delivery_commands_reject_unknown_fields_and_missing_keys() -> None:
    app = FastAPI()
    api.install(
        app, service_provider=lambda: _Service(), store_provider=lambda: _Store(),
        identity_provider=lambda *_args: None,
    )
    http = TestClient(app)
    base = (
        f"/api/projects/{PROJECT}/workspaces/{WORKSPACE}/work-items/{WORK}"
    )
    invalid = http.post(
        base + "/apply",
        json={"expected_delivery_revision": 1, "auto": True},
        headers={"Idempotency-Key": "apply"},
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_argument"
    missing = http.post(
        base + "/apply", json={"expected_delivery_revision": 1},
    )
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "idempotency_key_required"
    wrong_type = http.post(
        base + "/apply", json={"expected_delivery_revision": "1"},
        headers={"Idempotency-Key": "wrong-type"},
    )
    assert wrong_type.status_code == 400
    assert wrong_type.json()["error"]["code"] == "invalid_argument"
    duplicate = http.post(
        base + "/apply", content=b'{"expected_delivery_revision":1,'
        b'"expected_delivery_revision":1}',
        headers={
            "Idempotency-Key": "duplicate", "Content-Type": "application/json",
        },
    )
    assert duplicate.status_code == 400
    assert duplicate.json()["error"]["code"] == "invalid_argument"
