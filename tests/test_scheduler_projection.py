"""SCHED-002 ordinary projection tests. Red-first cases R01–R24."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_cockpit.scheduler_projection import (
    DISPATCH_DEPENDENCIES,
    ProjectionError,
    SchedulerProjection,
    apply_lease_heartbeat,
    eligible_pairs,
    loads_snapshot,
    project_scheduler_projection,
    second_active_lease_allowed,
    stamp_revisions,
    validate_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/scheduler_v1"
T0 = datetime(2026, 8, 13, 9, 0, tzinfo=timezone.utc)
FORBIDDEN = {"lease_conflict", "lease_claim_timeout"}


class FakeAdapters:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start_agent(self, *_a, **_k) -> None:
        self.calls.append("start_agent")

    def pane_send(self, *_a, **_k) -> None:
        self.calls.append("pane_send")

    def merge(self, *_a, **_k) -> None:
        self.calls.append("merge")

    def accept(self, *_a, **_k) -> None:
        self.calls.append("accept")

    def close_assignment(self, *_a, **_k) -> None:
        self.calls.append("close_assignment")

    def write_delivery(self, *_a, **_k) -> None:
        self.calls.append("write_delivery")

    def prompt_send(self, *_a, **_k) -> None:
        self.calls.append("prompt_send")

    def mail_send(self, *_a, **_k) -> None:
        self.calls.append("mail_send")

    def claim_lease(self, *_a, **_k) -> None:
        self.calls.append("claim_lease")

    def heartbeat_lease(self, *_a, **_k) -> None:
        self.calls.append("heartbeat_lease")

    def dispatch_offer(self, *_a, **_k) -> None:
        self.calls.append("dispatch_offer")

    def git_mutate(self, *_a, **_k) -> None:
        self.calls.append("git_mutate")

    def sqlite_write(self, *_a, **_k) -> None:
        self.calls.append("sqlite_write")

    def herdr_start(self, *_a, **_k) -> None:
        self.calls.append("herdr_start")

    def attention_write(self, *_a, **_k) -> None:
        self.calls.append("attention_write")


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _snap(name: str) -> dict:
    return deepcopy(_fixture(name)["snapshot"])


def _ready_agent(*, instance: str, verified: str = "claude", observed: str = "claude",
                 identity: str | None = "id-1", generation: int | None = 1,
                 attachment: str | None = "att-1", state: str = "idle") -> dict:
    return {
        "agent_instance_id": instance,
        "identity_revision": identity,
        "attachment_id": attachment,
        "attachment_revision": "att-rev-1" if attachment else None,
        "runtime_generation": generation,
        "observed_harness": observed,
        "verified_harness": verified,
        "observed_state": state,
        "projected_state": "unavailable",
        "observed_at": "2026-08-13T09:00:00+00:00",
        "freshness_deadline": "2026-08-13T10:00:00+00:00",
        "source_revision": "herdr-rev-1",
        "reason_codes": [],
    }


def _project(snapshot: dict, *, evaluated_at=T0, previous=None) -> SchedulerProjection:
    return project_scheduler_projection(
        stamp_revisions(snapshot), evaluated_at=evaluated_at, previous=previous,
    )


def test_r01_public_symbol_is_pure_function():
    assert callable(project_scheduler_projection)
    assert project_scheduler_projection.__name__ == "project_scheduler_projection"


def test_r02_pinned_a75e8c4_capacity_and_ready():
    result = _project(_snap("pinned_a75e8c4.json"))
    assert result.capacity.effective == 3
    assert result.capacity.active == 1
    assert result.capacity.remaining == 2
    assert [item.source_id for item in result.work.ready] == [
        "PROJ-004-project-api", "SCHED-001-authority-contract",
    ]
    assert result.source.plan_sha256 == (
        "sha256:b555dd4f36fa1ed3cdfe6ef22f338d26efdeb4a1f42d577f2f8fb202ab991c54"
    )
    assert result.dispatch.available is False
    assert result.input_fingerprint.startswith("sha256:")


def test_r03_parent_fingerprint_and_ready_differ():
    current = _project(_snap("pinned_a75e8c4.json"))
    parent = _project(_snap("pinned_parent_0bbbc56.json"))
    assert current.input_fingerprint != parent.input_fingerprint
    assert parent.capacity.effective == 3
    assert parent.capacity.active == 2
    assert parent.capacity.remaining == 1
    assert [item.source_id for item in parent.work.ready] == ["SCHED-001-authority-contract"]


def test_r04_same_input_is_byte_equivalent_and_does_not_mutate():
    snapshot = _snap("pinned_a75e8c4.json")
    before = json.dumps(snapshot, sort_keys=True)
    first = _project(snapshot)
    second = _project(snapshot)
    assert first == second
    assert json.dumps(snapshot, sort_keys=True) == before


def test_r05_clock_only_change_keeps_fingerprint():
    snapshot = _snap("pinned_a75e8c4.json")
    first = _project(snapshot, evaluated_at=T0)
    later = _project(snapshot, evaluated_at=T0 + timedelta(seconds=15))
    assert first.input_fingerprint == later.input_fingerprint
    assert later.next_reconciliation_at == T0 + timedelta(seconds=60)


def test_r06_schema_failures_are_source_invalid():
    extra = _snap("valid_minimal.json")
    extra["unexpected"] = True
    with pytest.raises(ProjectionError, match="source_invalid"):
        project_scheduler_projection(extra, evaluated_at=T0)
    missing = _snap("valid_minimal.json")
    del missing["source"]["revision"]
    with pytest.raises(ProjectionError, match="source_invalid"):
        project_scheduler_projection(missing, evaluated_at=T0)
    bool_int = _snap("valid_minimal.json")
    bool_int["capacity"]["effective_writer_wip"] = True
    with pytest.raises(ProjectionError, match="source_invalid"):
        project_scheduler_projection(bool_int, evaluated_at=T0)
    text = (FIXTURES / "valid_minimal.json").read_text()
    raw = text.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 2', 1)
    with pytest.raises(ProjectionError, match="source_invalid"):
        loads_snapshot(raw)


def test_r07_five_source_states_do_not_mix():
    mapping = {
        "source_unavailable.json": ("unavailable", "unknown", "source_unavailable"),
        "source_invalid.json": ("invalid", "unknown", "source_invalid"),
        "source_ambiguous.json": ("ambiguous", "unknown", "authoritative_plan_ambiguous"),
        "source_stale.json": ("available", "unknown", None),
    }
    for name, (status, freshness, reason) in mapping.items():
        result = _project(_snap(name))
        assert result.source.status == status
        assert result.source.freshness == freshness
        if reason is not None:
            assert reason in result.reason_codes
        else:
            assert "source_stale" not in result.reason_codes
        assert eligible_pairs(stamp_revisions(_snap(name)), T0) == ()
        assert result.dispatch.available is False
    dirty = stamp_revisions(_snap("valid_minimal.json") | {})
    dirty["source"]["git_dirty"] = True
    dirty = stamp_revisions(dirty)
    result = _project(dirty)
    assert result.source.freshness == "dirty"
    assert "source_dirty" in result.reason_codes
    assert "source_invalid" not in result.reason_codes
    assert eligible_pairs(dirty, T0) == ()


def test_r08_nonzero_dispatch_count_is_invalid():
    snapshot = _snap("valid_minimal.json")
    snapshot["active_dispatch_count"] = 1
    with pytest.raises(ProjectionError, match="source_invalid"):
        _project(snapshot)


def test_r09_no_previous_cannot_open_idle_alert():
    snapshot = _snap("valid_minimal.json")
    snapshot["work"][0]["sensitivity"] = "ordinary"
    snapshot["agents"] = [_ready_agent(instance="i-aaaaaaaaaaaaaaaaaaaaaaaaaa")]
    result = _project(snapshot)
    assert [alert.status for alert in result.alerts] == ["absent"]
    assert result.alerts[0].reason_code == "timing_unavailable"
    assert "ready_agent_idle_undispatched" not in result.reason_codes
    assert "timing_unavailable" in result.reason_codes


def test_r10_injected_first_observed_opens_at_90_not_89():
    snapshot = _snap("valid_minimal.json")
    snapshot["work"][0]["sensitivity"] = "ordinary"
    snapshot["agents"] = [_ready_agent(instance="i-aaaaaaaaaaaaaaaaaaaaaaaaaa")]
    prior = {"first_observed_at": "2026-08-13T09:00:00+00:00"}
    pending = _project(snapshot, evaluated_at=T0 + timedelta(seconds=89.999), previous=prior)
    assert pending.alerts[0].status == "pending"
    assert "ready_agent_idle_undispatched" not in pending.reason_codes
    opened = _project(snapshot, evaluated_at=T0 + timedelta(seconds=90), previous=prior)
    assert opened.alerts[0].status == "open"
    assert "ready_agent_idle_undispatched" in opened.reason_codes


def test_r11_source_gap_resets_alert_age():
    snapshot = _snap("valid_minimal.json")
    snapshot["work"][0]["sensitivity"] = "ordinary"
    snapshot["agents"] = [_ready_agent(instance="i-aaaaaaaaaaaaaaaaaaaaaaaaaa")]
    snapshot["source"]["freshness_deadline"] = "2026-08-13T08:59:59+00:00"
    prior = {"first_observed_at": "2026-08-13T09:00:00+00:00"}
    result = _project(snapshot, evaluated_at=T0 + timedelta(seconds=90), previous=prior)
    assert result.source.freshness == "unknown"
    assert all(alert.status != "open" for alert in result.alerts)


def test_r12_unclassified_ready_has_zero_pairs():
    snapshot = _snap("valid_minimal.json")
    snapshot["agents"] = [_ready_agent(instance="i-aaaaaaaaaaaaaaaaaaaaaaaaaa")]
    result = _project(snapshot)
    assert eligible_pairs(stamp_revisions(snapshot), T0) == ()
    assert "sensitive_route_unavailable" in result.reason_codes
    assert result.dispatch.available is False


def test_r13_verified_kimi_pairs_sensitive_but_stays_readonly():
    snapshot = _snap("valid_minimal.json")
    snapshot["work"][0]["sensitivity"] = "sensitive_security"
    snapshot["agents"] = [_ready_agent(instance="i-aaaaaaaaaaaaaaaaaaaaaaaaaa", verified="kimi", observed="kimi")]
    result = _project(snapshot)
    assert eligible_pairs(stamp_revisions(snapshot), T0) == (("CAR-READY", "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"),)
    assert result.dispatch.available is False
    assert result.dispatch.reason_code == "readonly_projection_only"


def test_r14_sensitive_denies_opencode_alias_and_mismatch():
    base = _snap("valid_minimal.json")
    base["work"][0]["sensitivity"] = "sensitive_security"
    denied = [
        _ready_agent(instance="i-opencode", verified="opencode", observed="opencode"),
        _ready_agent(instance="i-alias", verified="unknown", observed="kimi-code"),
        _ready_agent(instance="i-mismatch", verified="opencode", observed="claude"),
    ]
    for agent in denied:
        snapshot = deepcopy(base)
        snapshot["agents"] = [agent]
        result = _project(snapshot)
        assert eligible_pairs(stamp_revisions(snapshot), T0) == ()
        assert "agent_policy_denied" in result.reason_codes


def test_r15_ready_and_idle_without_edge_is_not_undispatched():
    snapshot = _snap("valid_minimal.json")
    snapshot["work"][0]["sensitivity"] = "sensitive_security"
    snapshot["agents"] = [_ready_agent(instance="i-opencode", verified="opencode", observed="opencode")]
    result = _project(snapshot)
    assert "ready_but_no_eligible_agent" in result.reason_codes
    assert "ready_agent_idle_undispatched" not in result.reason_codes
    assert result.alerts[0].kind == "ready_but_no_eligible_agent"


def test_r16_occupancy_union_no_double_count_and_review_wip():
    union = _project(_snap("scenario_occupancy_union.json"))
    assert union.capacity.effective == 1
    assert union.capacity.active == 1
    assert union.capacity.remaining == 0
    once = _project(_snap("scenario_occupancy_no_double_count.json"))
    assert once.capacity.active == 1
    assert once.capacity.remaining == 0
    review = _project(_snap("scenario_review_occupies_wip.json"))
    assert review.capacity.effective == 2
    assert review.capacity.active == 2
    assert review.capacity.remaining == 0
    assert "writer_wip_full" in {
        code for item in review.work.ready for code in item.reason_codes
    }


def test_r17_r1_lease_blocks_r2():
    snapshot = _snap("scenario_lease_r1_blocks_r2.json")
    assert second_active_lease_allowed(snapshot) is False


def test_r18_reviewer_excludes_author_and_leased_agent():
    snapshot = _snap("valid_minimal.json")
    snapshot["work"][0].update({
        "phase": "reviewer", "state": "review",
        "author_agent_instance_id": "i-author", "sensitivity": "ordinary",
    })
    snapshot["agents"] = [
        _ready_agent(instance="i-author"),
        _ready_agent(instance="i-leased"),
        _ready_agent(instance="i-reviewer"),
    ]
    snapshot["active_leases"] = [{
        "lease_id": "lease-1", "project_id": "agent-cockpit-next", "workspace_id": "ws-1",
        "source_kind": "delivery_car", "source_id": "CAR-READY", "source_revision": "rev-1",
        "phase": "writer", "agent_instance_id": "i-leased", "status": "working",
    }]
    assert eligible_pairs(stamp_revisions(snapshot), T0) == (("CAR-READY", "i-reviewer"),)


def test_r19_pane_heartbeat_is_ignored_generation_renews():
    lease = {"lease_id": "lease-1", "runtime_generation": 3, "expires_at": "2026-08-13T09:01:00+00:00"}
    pane = apply_lease_heartbeat(lease, {
        "kind": "pane", "lease_id": "lease-1", "runtime_generation": 3,
        "expires_at": "2026-08-13T09:10:00+00:00",
    })
    old = apply_lease_heartbeat(lease, {
        "kind": "generation_credential", "lease_id": "lease-1", "runtime_generation": 2,
        "expires_at": "2026-08-13T09:10:00+00:00",
    })
    renewed = apply_lease_heartbeat(lease, {
        "kind": "generation_credential", "lease_id": "lease-1", "runtime_generation": 3,
        "expires_at": "2026-08-13T09:10:00+00:00",
    })
    assert pane == lease == old
    assert renewed["expires_at"] == "2026-08-13T09:10:00+00:00"


def test_r20_unlinked_kept_and_linked_not_closed():
    unlinked = _project(_snap("scenario_unlinked_authorities.json"))
    assert "unlinked_authorities" in unlinked.reason_codes
    linked = _project(_snap("scenario_linked_no_sync.json"))
    assert "unlinked_authorities" not in linked.reason_codes
    assert [item["status"] for item in _snap("scenario_linked_no_sync.json")["assignments"]] == ["in_progress"]


def test_r21_pane_done_does_not_create_review_work():
    result = _project(_snap("scenario_pane_done_no_review.json"))
    assert result.work.ready == ()
    assert result.work.review == ()
    assert "review_authority_unavailable" in result.reason_codes


def test_r22_missing_dependencies_are_complete_and_dispatch_false():
    result = _project(_snap("valid_minimal.json"))
    assert result.dispatch.available is False
    assert result.dispatch.mode == "readonly"
    assert result.dispatch.reason_code == "readonly_projection_only"
    assert result.dispatch.missing_dependencies == DISPATCH_DEPENDENCIES
    assert len(DISPATCH_DEPENDENCIES) == 11


def test_r23_fake_adapters_are_never_invoked():
    adapters = FakeAdapters()
    for name in ("pinned_a75e8c4.json", "valid_minimal.json", "scenario_linked_no_sync.json"):
        _project(_snap(name))
        apply_lease_heartbeat(
            {"lease_id": "x", "runtime_generation": 1, "expires_at": "2026-08-13T09:01:00+00:00"},
            {"kind": "pane", "lease_id": "x", "runtime_generation": 1, "expires_at": "2026-08-13T09:02:00+00:00"},
        )
    assert adapters.calls == []
    assert FORBIDDEN.isdisjoint(_project(_snap("valid_minimal.json")).reason_codes)


def test_r24_output_is_deterministically_sorted():
    snapshot = _snap("valid_minimal.json")
    snapshot["work"] = [
        {**snapshot["work"][0], "source_id": "Z-READY", "reason_codes": ["writer_wip_full", "dependency_waiting"]},
        {**snapshot["work"][0], "source_id": "A-READY", "reason_codes": ["dependency_waiting"]},
    ]
    snapshot["agents"] = [
        _ready_agent(instance="i-zzzzzzzzzzzzzzzzzzzzzzzzzz"),
        _ready_agent(instance="i-aaaaaaaaaaaaaaaaaaaaaaaaaa"),
    ]
    result = _project(snapshot)
    assert [item.source_id for item in result.work.ready] == ["A-READY", "Z-READY"]
    assert result.work.ready[0].reason_codes == ("dependency_waiting",)
    assert [item.agent_instance_id for item in result.agents.states] == [
        "i-aaaaaaaaaaaaaaaaaaaaaaaaaa", "i-zzzzzzzzzzzzzzzzzzzzzzzzzz",
    ]
    assert list(result.reason_codes) == sorted(result.reason_codes)


def _ordinary_ready(extra_work=None, agents=None, leases=None) -> dict:
    snapshot = _snap("valid_minimal.json")
    snapshot["work"][0]["sensitivity"] = "ordinary"
    if extra_work:
        snapshot["work"].extend(extra_work)
    snapshot["agents"] = agents or []
    snapshot["active_leases"] = leases or []
    return stamp_revisions(snapshot)


def test_c1_writer_pairing_requires_idle():
    for state in ("working", "blocked", "done", "unknown"):
        snapshot = _ordinary_ready(agents=[_ready_agent(instance="i-aaaaaaaaaaaaaaaaaaaaaaaaaa", state=state)])
        assert eligible_pairs(snapshot, T0) == ()
        result = project_scheduler_projection(snapshot, evaluated_at=T0)
        assert ("CAR-READY", "i-aaaaaaaaaaaaaaaaaaaaaaaaaa") not in eligible_pairs(snapshot, T0)
        assert result.dispatch.available is False
    idle = _ordinary_ready(agents=[_ready_agent(instance="i-aaaaaaaaaaaaaaaaaaaaaaaaaa", state="idle")])
    assert eligible_pairs(idle, T0) == (("CAR-READY", "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"),)


def test_c2_agent_is_single_active_across_work_and_phase():
    other = {
        **_snap("valid_minimal.json")["work"][0],
        "source_id": "OTHER-CAR",
        "state": "active",
        "sensitivity": "ordinary",
    }
    snapshot = _ordinary_ready(
        extra_work=[other],
        agents=[_ready_agent(instance="i-aaaaaaaaaaaaaaaaaaaaaaaaaa")],
        leases=[{
            "lease_id": "lease-other", "project_id": "agent-cockpit-next", "workspace_id": "ws-1",
            "source_kind": "delivery_car", "source_id": "OTHER-CAR", "source_revision": "rev-1",
            "phase": "writer", "agent_instance_id": "i-aaaaaaaaaaaaaaaaaaaaaaaaaa", "status": "working",
        }],
    )
    assert eligible_pairs(snapshot, T0) == ()


def test_c3_work_with_active_lease_does_not_pair_another_agent():
    snapshot = _ordinary_ready(
        agents=[
            _ready_agent(instance="i-firstaaaaaaaaaaaaaaaaaaaaaaa"),
            _ready_agent(instance="i-secondaaaaaaaaaaaaaaaaaaaaaa"),
        ],
        leases=[{
            "lease_id": "offer-1", "project_id": "agent-cockpit-next", "workspace_id": "ws-1",
            "source_kind": "delivery_car", "source_id": "CAR-READY", "source_revision": "rev-1",
            "phase": "writer", "agent_instance_id": "i-firstaaaaaaaaaaaaaaaaaaaaaaa", "status": "offered",
        }],
    )
    pairs = eligible_pairs(snapshot, T0)
    assert "i-secondaaaaaaaaaaaaaaaaaaaaaa" not in {agent for _work, agent in pairs}
    assert pairs == ()


def test_c4_missing_verified_harness_is_not_available_or_paired():
    snapshot = _ordinary_ready(agents=[
        _ready_agent(instance="i-aaaaaaaaaaaaaaaaaaaaaaaaaa", verified=None),
    ])
    result = project_scheduler_projection(snapshot, evaluated_at=T0)
    assert eligible_pairs(snapshot, T0) == ()
    assert result.agents.available_count == 0
    assert result.agents.states[0].projected_state != "available"
    assert "runtime_not_verified" in result.agents.states[0].reason_codes


def test_c5_typed_link_requires_matching_kind_and_id():
    snapshot = _snap("valid_minimal.json")
    snapshot["work"][0]["typed_links"] = [{"kind": "review_packet", "id": "CAR-READY"}]
    with pytest.raises(ProjectionError, match="source_invalid"):
        validate_snapshot(stamp_revisions(snapshot))
    with pytest.raises(ProjectionError, match="source_invalid"):
        project_scheduler_projection(stamp_revisions(snapshot), evaluated_at=T0)
    assignment_as_work = _snap("valid_minimal.json")
    assignment_as_work["assignments"] = [{
        "assignment_id": "ASN-A", "source_revision": "asn-1", "status": "assigned",
        "assignee": "i-aaaaaaaaaaaaaaaaaaaaaaaaaa",
        "typed_links": [{"kind": "assignment", "id": "CAR-READY"}],
    }]
    with pytest.raises(ProjectionError, match="source_invalid"):
        validate_snapshot(stamp_revisions(assignment_as_work))


def test_c6_previous_without_onset_is_absent_timing_unavailable():
    snapshot = _ordinary_ready(agents=[_ready_agent(instance="i-aaaaaaaaaaaaaaaaaaaaaaaaaa")])
    for previous in (None, {}, SchedulerProjection(
        schema_version=1, project_id="agent-cockpit-next", input_fingerprint="sha256:" + "0" * 64,
        evaluated_at=T0, next_reconciliation_at=T0 + timedelta(seconds=45),
        source=project_scheduler_projection(snapshot, evaluated_at=T0).source,
        capacity=project_scheduler_projection(snapshot, evaluated_at=T0).capacity,
        work=project_scheduler_projection(snapshot, evaluated_at=T0).work,
        agents=project_scheduler_projection(snapshot, evaluated_at=T0).agents,
        dispatch=project_scheduler_projection(snapshot, evaluated_at=T0).dispatch,
        alerts=(),
        reason_codes=("readonly_projection_only",),
    )):
        result = project_scheduler_projection(snapshot, evaluated_at=T0, previous=previous)
        assert any(alert.status == "absent" and alert.reason_code == "timing_unavailable" for alert in result.alerts)
        assert "timing_unavailable" in result.reason_codes
        assert all(alert.status != "open" for alert in result.alerts)


def test_c7_open_condition_disappearance_emits_one_resolved():
    snapshot = _ordinary_ready(agents=[_ready_agent(instance="i-aaaaaaaaaaaaaaaaaaaaaaaaaa")])
    opened = project_scheduler_projection(
        snapshot, evaluated_at=T0 + timedelta(seconds=90),
        previous={"first_observed_at": "2026-08-13T09:00:00+00:00"},
    )
    assert any(alert.status == "open" for alert in opened.alerts)
    stale = deepcopy(snapshot)
    stale["source"]["freshness_deadline"] = "2026-08-13T08:59:59+00:00"
    stale = stamp_revisions(stale)
    resolved = project_scheduler_projection(stale, evaluated_at=T0 + timedelta(seconds=90), previous=opened)
    assert [alert.status for alert in resolved.alerts] == ["resolved"]
    assert resolved.alerts[0].status == "resolved"
    assert all(alert.status != "open" for alert in resolved.alerts)


def test_c8_ordinary_ready_without_eligible_agent_emits_reason():
    snapshot = _ordinary_ready(agents=[
        _ready_agent(instance="i-aaaaaaaaaaaaaaaaaaaaaaaaaa", identity=None),
    ])
    result = project_scheduler_projection(snapshot, evaluated_at=T0)
    assert eligible_pairs(snapshot, T0) == ()
    assert "ready_but_no_eligible_agent" in result.reason_codes
    assert "ready_agent_idle_undispatched" not in result.reason_codes
    assert result.capacity.remaining > 0


def test_c9_non_available_source_is_unknown_even_when_dirty():
    for status, reason in (
        ("unavailable", "source_unavailable"),
        ("invalid", "source_invalid"),
        ("ambiguous", "authoritative_plan_ambiguous"),
    ):
        snapshot = _snap("valid_minimal.json")
        snapshot["source"]["status"] = status
        snapshot["source"]["git_dirty"] = True
        snapshot = stamp_revisions(snapshot)
        result = project_scheduler_projection(snapshot, evaluated_at=T0)
        assert result.source.freshness == "unknown"
        assert reason in result.reason_codes
        assert result.source.freshness != "dirty"


def test_c10_unclassified_empty_previous_emits_timing_unavailable_alert():
    snapshot = stamp_revisions(_snap("valid_minimal.json"))
    assert eligible_pairs(snapshot, T0) == ()
    result = project_scheduler_projection(snapshot, evaluated_at=T0, previous={})
    assert "ready_but_no_eligible_agent" in result.reason_codes
    assert any(
        alert.status == "absent" and alert.reason_code == "timing_unavailable"
        for alert in result.alerts
    )
    assert any(alert.reason_code == "ready_but_no_eligible_agent" for alert in result.alerts)


def test_f1_resolved_recovery_requires_new_onset():
    snapshot = _ordinary_ready(agents=[_ready_agent(instance="i-aaaaaaaaaaaaaaaaaaaaaaaaaa")])
    opened = project_scheduler_projection(
        snapshot, evaluated_at=T0 + timedelta(seconds=90),
        previous={"first_observed_at": "2026-08-13T09:00:00+00:00"},
    )
    assert any(alert.status == "open" for alert in opened.alerts)
    disappeared = deepcopy(snapshot)
    disappeared["source"]["freshness_deadline"] = "2026-08-13T09:01:29+00:00"
    disappeared = stamp_revisions(disappeared)
    resolved = project_scheduler_projection(
        disappeared, evaluated_at=T0 + timedelta(seconds=90), previous=opened,
    )
    assert [alert.status for alert in resolved.alerts] == ["resolved"]
    assert resolved.alerts[0].first_observed_at is None
    recovered = project_scheduler_projection(
        snapshot, evaluated_at=T0 + timedelta(seconds=180), previous=resolved,
    )
    assert any(
        alert.status == "absent" and alert.reason_code == "timing_unavailable"
        for alert in recovered.alerts
    )
    assert all(alert.status != "open" for alert in recovered.alerts)
    assert "ready_agent_idle_undispatched" not in recovered.reason_codes
    restarted = project_scheduler_projection(
        snapshot, evaluated_at=T0 + timedelta(seconds=270),
        previous={"first_observed_at": "2026-08-13T09:03:00+00:00"},
    )
    assert restarted.alerts[0].status == "open"
    assert "ready_agent_idle_undispatched" in restarted.reason_codes


def test_f2_reviewer_only_does_not_open_idle_alert_at_90s():
    snapshot = _snap("valid_minimal.json")
    snapshot["work"][0].update({
        "phase": "reviewer", "state": "review",
        "author_agent_instance_id": "i-author", "sensitivity": "ordinary",
    })
    snapshot["agents"] = [_ready_agent(instance="i-reviewer")]
    snapshot = stamp_revisions(snapshot)
    evaluated = T0 + timedelta(seconds=90)
    assert eligible_pairs(snapshot, evaluated) == (("CAR-READY", "i-reviewer"),)
    result = project_scheduler_projection(
        snapshot, evaluated_at=evaluated,
        previous={"first_observed_at": "2026-08-13T09:00:00+00:00"},
    )
    assert all(alert.status not in {"pending", "open"} for alert in result.alerts)
    assert "ready_agent_idle_undispatched" not in result.reason_codes
    writer = _ordinary_ready(agents=[_ready_agent(instance="i-aaaaaaaaaaaaaaaaaaaaaaaaaa")])
    opened = project_scheduler_projection(
        writer, evaluated_at=evaluated,
        previous={"first_observed_at": "2026-08-13T09:00:00+00:00"},
    )
    assert any(alert.status == "open" for alert in opened.alerts)
    reviewer_only = deepcopy(writer)
    reviewer_only["work"][0].update({
        "phase": "reviewer", "state": "review",
        "author_agent_instance_id": "i-author",
    })
    reviewer_only = stamp_revisions(reviewer_only)
    resolved = project_scheduler_projection(
        reviewer_only, evaluated_at=evaluated, previous=opened,
    )
    assert [alert.status for alert in resolved.alerts] == ["resolved"]
    assert resolved.alerts[0].first_observed_at is None


def test_f2_mixed_unpairable_writer_and_pairable_reviewer():
    snapshot = _snap("valid_minimal.json")
    template = snapshot["work"][0]
    snapshot["work"] = [
        {**template, "source_id": "CAR-WRITE", "phase": "writer", "state": "ready",
         "sensitivity": "sensitive_security"},
        {**template, "source_id": "CAR-REVIEW", "phase": "reviewer", "state": "ready",
         "sensitivity": "ordinary", "author_agent_instance_id": "i-author"},
    ]
    snapshot["agents"] = [
        _ready_agent(instance="i-reviewer", verified="opencode", observed="opencode"),
    ]
    snapshot = stamp_revisions(snapshot)
    evaluated = T0 + timedelta(seconds=90)
    pairs = eligible_pairs(snapshot, evaluated)
    assert pairs == (("CAR-REVIEW", "i-reviewer"),)
    result = project_scheduler_projection(
        snapshot, evaluated_at=evaluated,
        previous={"first_observed_at": "2026-08-13T09:00:00+00:00"},
    )
    assert all(alert.status not in {"pending", "open"} for alert in result.alerts)
    assert "ready_agent_idle_undispatched" not in result.reason_codes
    assert "ready_but_no_eligible_agent" in result.reason_codes
    assert any(alert.reason_code == "ready_but_no_eligible_agent" for alert in result.alerts)
    assert [item.source_id for item in result.work.ready] == ["CAR-REVIEW", "CAR-WRITE"]
    writer = _ordinary_ready(agents=[_ready_agent(instance="i-aaaaaaaaaaaaaaaaaaaaaaaaaa")])
    opened = project_scheduler_projection(
        writer, evaluated_at=evaluated,
        previous={"first_observed_at": "2026-08-13T09:00:00+00:00"},
    )
    assert any(alert.status == "open" for alert in opened.alerts)
    mixed_resolved = project_scheduler_projection(snapshot, evaluated_at=evaluated, previous=opened)
    assert [alert.status for alert in mixed_resolved.alerts] == ["resolved"]
    assert mixed_resolved.alerts[0].first_observed_at is None


def test_f3_observed_after_deadline_is_unknown():
    snapshot = _snap("valid_minimal.json")
    snapshot["source"]["observed_at"] = "2026-08-13T09:00:00+00:00"
    snapshot["source"]["freshness_deadline"] = "2026-08-13T08:59:59+00:00"
    snapshot = stamp_revisions(snapshot)
    result = project_scheduler_projection(snapshot, evaluated_at=T0)
    assert result.source.status == "available"
    assert result.source.freshness == "unknown"
    assert "source_stale" not in result.reason_codes
    assert result.source.freshness != "stale"


def test_f3_evaluated_before_observed_is_unknown():
    snapshot = stamp_revisions(_snap("valid_minimal.json"))
    result = project_scheduler_projection(
        snapshot, evaluated_at=T0 - timedelta(seconds=1),
    )
    assert result.source.status == "available"
    assert result.source.freshness == "unknown"
    assert "source_stale" not in result.reason_codes
    coherent = project_scheduler_projection(
        snapshot, evaluated_at=T0 + timedelta(minutes=5, seconds=1),
    )
    assert coherent.source.freshness == "stale"
    assert "source_stale" in coherent.reason_codes
