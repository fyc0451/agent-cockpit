"""Deterministic read-only scheduler projection. No I/O, timer, or dispatch."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

RECONCILIATION_INTERVAL_SECONDS = 45
READY_WITHOUT_DISPATCH_GRACE_SECONDS = 90
ONSET_TO_OPEN_UPPER_BOUND_SECONDS = 135

DISPATCH_DEPENDENCIES = (
    "SCHED-001-authority-contract",
    "SCHED-002-readonly-projection",
    "workspace_workitem_review_authority",
    "workspace_agent_identity_store",
    "runtime_attachment_generation_handshake",
    "author_gate_harness_catalog",
    "delivery_expected_revision_cas",
    "dispatch_lease_store",
    "reconciler_intents",
    "harness_durable_claim_heartbeat",
    "api_web_attention_wiring",
)

REASON_CODES = frozenset({
    "source_unavailable", "source_stale", "source_dirty", "source_invalid",
    "authoritative_plan_ambiguous", "dependency_waiting", "writer_wip_full",
    "scope_ownership_conflict", "review_backpressure", "reviewer_required",
    "author_gate_denied", "unlinked_authorities", "no_stable_identity",
    "runtime_not_verified", "runtime_generation_unavailable",
    "runtime_generation_drift", "recovery_required", "transport_unknown",
    "heartbeat_expired", "agent_cooldown", "agent_policy_denied",
    "sensitive_route_unavailable", "ready_but_no_eligible_agent",
    "ready_agent_idle_undispatched", "readonly_projection_only",
    "review_authority_unavailable", "timing_unavailable",
    "dispatch_dependency_missing",
})
SOURCE_STATUSES = frozenset({"available", "unavailable", "invalid", "ambiguous"})
SENSITIVITIES = frozenset({"ordinary", "sensitive_security", "unclassified"})
WORK_STATES = frozenset({"ready", "waiting", "review", "active", "accepted", "blocked"})
VERIFIED_HARNESS = frozenset({"claude", "kimi", "codex", "opencode", "zcode", "unknown"})
OBSERVED_HARNESS = VERIFIED_HARNESS | {"kimi-code"}
SENSITIVE_OK = frozenset({"claude", "kimi"})
SOURCE_KINDS = frozenset({"delivery_car", "work_item", "review_packet", "assignment"})
PHASES = frozenset({"writer", "reviewer"})
LEASE_STATUSES = frozenset({"offered", "claimed_pending_source", "working", "compensating", "terminal"})
OBSERVED_STATES = frozenset({"working", "blocked", "done", "idle", "unknown"})
PROJECTED_STATES = frozenset({
    "available", "working", "blocked", "paused", "recovery_required",
    "unknown_transport", "unavailable",
})
ASSIGNMENT_STATUSES = frozenset({"assigned", "in_progress", "blocked", "review", "closed"})
ACTIVE_LEASE = frozenset({"offered", "claimed_pending_source", "working"})
SNAPSHOT_KEYS = (
    "schema_version", "project_id", "source", "capacity", "work", "agents",
    "assignments", "active_leases", "active_dispatch_count", "evaluated_input_revision",
)
SOURCE_KEYS = (
    "repository_id", "plan_path", "git_head", "git_dirty", "plan_sha256",
    "status", "observed_at", "freshness_deadline", "revision", "reason_codes",
)
CAPACITY_KEYS = (
    "effective_writer_wip", "active_writer_count", "remaining_writer_capacity", "revision",
)
WORK_KEYS = (
    "source_kind", "source_id", "source_revision", "phase", "state", "waiting_on",
    "author_agent_instance_id", "sensitivity", "typed_links", "reason_codes",
)
AGENT_KEYS = (
    "agent_instance_id", "identity_revision", "attachment_id", "attachment_revision",
    "runtime_generation", "observed_harness", "verified_harness", "observed_state",
    "projected_state", "observed_at", "freshness_deadline", "source_revision", "reason_codes",
)
ASSIGNMENT_KEYS = ("assignment_id", "source_revision", "status", "assignee", "typed_links")
LEASE_KEYS = (
    "lease_id", "project_id", "workspace_id", "source_kind", "source_id",
    "source_revision", "phase", "agent_instance_id", "status",
)
LINK_KEYS = ("kind", "id")


class ProjectionError(ValueError):
    def __init__(self, reason: str = "source_invalid") -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SourceProjection:
    status: str
    freshness: str
    observed_at: datetime | None
    age_seconds: int | None
    revision: str
    plan_sha256: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class CapacityProjection:
    effective: int
    active: int
    remaining: int


@dataclass(frozen=True)
class WorkProjectionItem:
    source_id: str
    source_kind: str
    state: str
    waiting_on: tuple[str, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class WorkProjection:
    ready: tuple[WorkProjectionItem, ...]
    waiting: tuple[WorkProjectionItem, ...]
    review: tuple[WorkProjectionItem, ...]
    active: tuple[WorkProjectionItem, ...]


@dataclass(frozen=True)
class AgentProjectionItem:
    agent_instance_id: str | None
    projected_state: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class AgentProjection:
    available_count: int
    states: tuple[AgentProjectionItem, ...]
    reason_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class DispatchProjection:
    available: bool
    mode: str
    reason_code: str
    missing_dependencies: tuple[str, ...]


@dataclass(frozen=True)
class SchedulerAlertIntent:
    kind: str
    severity: str
    dedupe_key: str
    status: str
    first_observed_at: datetime | None
    observed_for_seconds: int
    reason_code: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class SchedulerProjection:
    schema_version: int
    project_id: str
    input_fingerprint: str
    evaluated_at: datetime
    next_reconciliation_at: datetime
    source: SourceProjection
    capacity: CapacityProjection
    work: WorkProjection
    agents: AgentProjection
    dispatch: DispatchProjection
    alerts: tuple[SchedulerAlertIntent, ...]
    reason_codes: tuple[str, ...]


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


def source_revision(source: Mapping[str, Any]) -> str:
    return digest({
        "repository_id": source["repository_id"],
        "plan_path": source["plan_path"],
        "git_head": source["git_head"],
        "git_dirty": source["git_dirty"],
        "plan_sha256": source["plan_sha256"],
    })


def input_fingerprint(snapshot: Mapping[str, Any]) -> str:
    return digest({
        "schema_version": snapshot["schema_version"],
        "project_id": snapshot["project_id"],
        "source": {key: snapshot["source"][key] for key in SOURCE_KEYS},
        "capacity": snapshot["capacity"],
        "work": snapshot["work"],
        "agents": snapshot["agents"],
        "assignments": snapshot["assignments"],
        "active_leases": snapshot["active_leases"],
        "active_dispatch_count": snapshot["active_dispatch_count"],
    })


def stamp_revisions(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    value = deepcopy(dict(snapshot))
    value["source"] = dict(value["source"])
    value["source"]["revision"] = source_revision(value["source"])
    value["evaluated_input_revision"] = input_fingerprint(value)
    return value


def loads_snapshot(text: str) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ProjectionError("source_invalid")
            value[key] = item
        return value
    try:
        parsed = json.loads(text, object_pairs_hook=unique_object)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, ProjectionError):
            raise
        raise ProjectionError("source_invalid") from None
    if not isinstance(parsed, dict):
        raise ProjectionError("source_invalid")
    if "snapshot" in parsed and isinstance(parsed["snapshot"], dict):
        return parsed["snapshot"]
    return parsed


def _fail() -> None:
    raise ProjectionError("source_invalid")


def _text(value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or value == "":
        _fail()
    return value  # type: ignore[return-value]


def _exact(value: object, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(keys):
        _fail()
    return value


def _list(value: object) -> list[Any]:
    if not isinstance(value, list):
        _fail()
    return value


def _reasons(value: object) -> tuple[str, ...]:
    items = _list(value)
    for item in items:
        if type(item) is not str or item not in REASON_CODES:
            _fail()
    return tuple(sorted(set(items)))


def _parse_utc(value: object) -> datetime | None:
    if value is None:
        return None
    if type(value) is not str:
        _fail()
    text = value
    if text.endswith("Z") or "+" not in text[-6:]:
        _fail()
    if not text.endswith("+00:00"):
        _fail()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        _fail()
    return parsed


def _sha(value: object) -> str:
    text = _text(value)
    if text is None or not text.startswith("sha256:") or len(text) != 71:
        _fail()
    hexpart = text[7:]
    if any(char not in "0123456789abcdef" for char in hexpart):
        _fail()
    return text


def _links(value: object) -> list[dict[str, Any]]:
    links = []
    for item in _list(value):
        link = _exact(item, LINK_KEYS)
        if link["kind"] not in SOURCE_KINDS:
            _fail()
        _text(link["id"])
        links.append(link)
    return links


def validate_snapshot(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != set(SNAPSHOT_KEYS):
        _fail()
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        _fail()
    _text(raw["project_id"])
    if type(raw["active_dispatch_count"]) is not int or raw["active_dispatch_count"] != 0:
        _fail()
    source = _exact(raw["source"], SOURCE_KEYS)
    for key in ("repository_id", "plan_path", "git_head", "revision"):
        _text(source[key])
    if source["status"] not in SOURCE_STATUSES:
        _fail()
    _sha(source["plan_sha256"])
    if type(source["git_dirty"]) is not bool:
        _fail()
    _parse_utc(source["observed_at"])
    _parse_utc(source["freshness_deadline"])
    _reasons(source["reason_codes"])
    if source["revision"] != source_revision(source):
        _fail()

    capacity = _exact(raw["capacity"], CAPACITY_KEYS)
    for key in ("effective_writer_wip", "active_writer_count", "remaining_writer_capacity"):
        if type(capacity[key]) is not int or capacity[key] < 0:
            _fail()
    _text(capacity["revision"])

    work_ids: list[str] = []
    work = []
    for item in _list(raw["work"]):
        item = _exact(item, WORK_KEYS)
        if item["source_kind"] not in SOURCE_KINDS or item["phase"] not in PHASES:
            _fail()
        _text(item["source_id"])
        _text(item["source_revision"])
        if item["author_agent_instance_id"] is not None:
            _text(item["author_agent_instance_id"])
        if any(type(value) is not str or not value for value in _list(item["waiting_on"])):
            _fail()
        _links(item["typed_links"])
        if item["state"] not in WORK_STATES or item["sensitivity"] not in SENSITIVITIES:
            _fail()
        _reasons(item["reason_codes"])
        work_ids.append(item["source_id"])
        work.append(item)
    if len(work_ids) != len(set(work_ids)):
        _fail()

    agents = []
    agent_ids: set[str] = set()
    for agent in _list(raw["agents"]):
        agent = _exact(agent, AGENT_KEYS)
        for key in ("identity_revision", "attachment_id", "attachment_revision", "source_revision"):
            _text(agent[key], nullable=True)
        if agent["agent_instance_id"] is not None:
            _text(agent["agent_instance_id"])
            if agent["agent_instance_id"] in agent_ids:
                _fail()
            agent_ids.add(agent["agent_instance_id"])
        if agent["runtime_generation"] is not None and (
            type(agent["runtime_generation"]) is not int or agent["runtime_generation"] < 0
        ):
            _fail()
        if agent["observed_harness"] not in OBSERVED_HARNESS:
            _fail()
        if agent["observed_state"] not in OBSERVED_STATES or agent["projected_state"] not in PROJECTED_STATES:
            _fail()
        verified = agent["verified_harness"]
        if verified is not None and verified not in VERIFIED_HARNESS:
            _fail()
        _reasons(agent["reason_codes"])
        _parse_utc(agent["observed_at"])
        _parse_utc(agent["freshness_deadline"])
        agents.append(agent)

    assignments = []
    assignment_ids: set[str] = set()
    for assignment in _list(raw["assignments"]):
        assignment = _exact(assignment, ASSIGNMENT_KEYS)
        _text(assignment["assignment_id"])
        _text(assignment["source_revision"])
        _text(assignment["assignee"], nullable=True)
        _links(assignment["typed_links"])
        if assignment["assignment_id"] in assignment_ids:
            _fail()
        assignment_ids.add(assignment["assignment_id"])
        if assignment["status"] not in ASSIGNMENT_STATUSES:
            _fail()
        assignments.append(assignment)

    leases = []
    for lease in _list(raw["active_leases"]):
        lease = _exact(lease, LEASE_KEYS)
        for key in LEASE_KEYS:
            if key in {"phase", "status", "source_kind"}:
                continue
            _text(lease[key])
        if lease["source_kind"] not in SOURCE_KINDS or lease["phase"] not in PHASES:
            _fail()
        if lease["status"] not in LEASE_STATUSES:
            _fail()
        leases.append(lease)

    work_by_id = {item["source_id"]: item for item in work}
    for item in [*work, *assignments]:
        for link in item["typed_links"]:
            known = assignment_ids if link["kind"] == "assignment" else set(work_by_id)
            if link["id"] not in known:
                _fail()
    for lease in leases:
        leased = work_by_id.get(lease["source_id"])
        if leased is None or lease["source_kind"] != leased["source_kind"]:
            _fail()
    if raw["evaluated_input_revision"] != input_fingerprint(raw):
        _fail()
    return {
        **raw,
        "source": source,
        "capacity": capacity,
        "work": work,
        "agents": agents,
        "assignments": assignments,
        "active_leases": leases,
    }


def freshness(source: Mapping[str, Any], evaluated_at: datetime) -> str:
    if source["git_dirty"] is True:
        return "dirty"
    status = source["status"]
    observed = _parse_utc(source["observed_at"])
    deadline = _parse_utc(source["freshness_deadline"])
    if status == "available" and observed is not None and deadline is not None:
        if evaluated_at <= deadline:
            return "fresh"
        return "stale"
    return "unknown"


def occupancy(snapshot: Mapping[str, Any]) -> int:
    active_ids = {item["source_id"] for item in snapshot["work"] if item["state"] in {"active", "review"}}
    reserved = {
        lease["source_id"]
        for lease in snapshot["active_leases"]
        if lease["status"] in ACTIVE_LEASE and lease["phase"] == "writer"
    }
    still_open = {
        item["source_id"]
        for item in snapshot["work"]
        if item["state"] in {"ready", "waiting", "active", "review"}
    }
    return len(active_ids | (reserved & still_open))


def second_active_lease_allowed(snapshot: Mapping[str, Any]) -> bool:
    seen_work: set[tuple[Any, ...]] = set()
    seen_agent: set[str] = set()
    for lease in snapshot["active_leases"]:
        if lease["status"] not in ACTIVE_LEASE:
            continue
        key = (
            lease["project_id"], lease["workspace_id"], lease["source_kind"],
            lease["source_id"], lease["phase"],
        )
        if key in seen_work or lease["agent_instance_id"] in seen_agent:
            return False
        seen_work.add(key)
        seen_agent.add(lease["agent_instance_id"])
    for lease in snapshot["active_leases"]:
        for item in snapshot["work"]:
            if (
                lease["source_id"] == item["source_id"]
                and lease["source_revision"] != item["source_revision"]
                and lease["status"] in ACTIVE_LEASE
            ):
                return False
    return True


def _agent_reasons(agent: Mapping[str, Any], evaluated_at: datetime) -> list[str]:
    reasons = list(agent.get("reason_codes") or [])
    if not agent.get("identity_revision") or not agent.get("agent_instance_id"):
        reasons.append("no_stable_identity")
    if agent.get("runtime_generation") is None or not agent.get("attachment_id"):
        reasons.append("runtime_generation_unavailable")
        reasons.append("runtime_not_verified")
    if agent.get("projected_state") == "unknown_transport" or agent.get("observed_state") == "unknown":
        reasons.append("transport_unknown")
    observed = _parse_utc(agent.get("observed_at"))
    deadline = _parse_utc(agent.get("freshness_deadline"))
    if observed is None or deadline is None or evaluated_at > deadline:
        reasons.append("source_stale")
    return sorted(set(reasons))


_BLOCKING = {
    "no_stable_identity", "runtime_generation_unavailable", "runtime_not_verified",
    "transport_unknown", "source_stale",
}


def eligible_pairs(snapshot: Mapping[str, Any], evaluated_at: datetime) -> tuple[tuple[str, str], ...]:
    if snapshot["source"]["status"] != "available":
        return ()
    if freshness(snapshot["source"], evaluated_at) != "fresh":
        return ()
    if occupancy(snapshot) >= snapshot["capacity"]["effective_writer_wip"]:
        writer_ok = False
    else:
        writer_ok = True
    pairs: list[tuple[str, str]] = []
    if writer_ok:
        for item in snapshot["work"]:
            if item["state"] != "ready" or item["phase"] != "writer":
                continue
            if item["sensitivity"] == "unclassified":
                continue
            for agent in snapshot["agents"]:
                reasons = _agent_reasons(agent, evaluated_at)
                if set(reasons) & _BLOCKING:
                    continue
                if item["sensitivity"] == "sensitive_security":
                    if agent.get("verified_harness") not in SENSITIVE_OK:
                        continue
                elif agent.get("verified_harness") in {None, "unknown"}:
                    continue
                instance = agent.get("agent_instance_id")
                if not instance:
                    continue
                pairs.append((item["source_id"], instance))
    leased = {
        lease["agent_instance_id"]
        for lease in snapshot["active_leases"]
        if lease["status"] in ACTIVE_LEASE
    }
    for item in snapshot["work"]:
        if item["phase"] != "reviewer" or item["state"] not in {"ready", "review"}:
            continue
        for agent in snapshot["agents"]:
            instance = agent.get("agent_instance_id")
            if not instance:
                continue
            if item.get("author_agent_instance_id") == instance:
                continue
            if instance in leased:
                continue
            if set(_agent_reasons(agent, evaluated_at)) & _BLOCKING:
                continue
            pairs.append((item["source_id"], instance))
    return tuple(pairs)


def apply_lease_heartbeat(lease_state: Mapping[str, Any], event: Mapping[str, Any]) -> dict[str, Any]:
    before = deepcopy(dict(lease_state))
    if event.get("kind") != "generation_credential":
        return before
    if event.get("lease_id") != before.get("lease_id"):
        return before
    if event.get("runtime_generation") != before.get("runtime_generation"):
        return before
    expires_at = event.get("expires_at")
    try:
        if _parse_utc(expires_at) is None:
            return before
    except ProjectionError:
        return before
    before["expires_at"] = expires_at
    return before


def _first_observed(previous: object) -> datetime | None:
    if previous is None:
        return None
    if isinstance(previous, Mapping) and previous.get("first_observed_at"):
        return _parse_utc(previous["first_observed_at"])
    if isinstance(previous, SchedulerProjection):
        for alert in previous.alerts:
            if alert.first_observed_at is not None:
                return alert.first_observed_at
    return None


def project_scheduler_projection(
    snapshot: Mapping[str, Any],
    *,
    evaluated_at: datetime,
    previous: SchedulerProjection | Mapping[str, Any] | None = None,
) -> SchedulerProjection:
    validated = validate_snapshot(deepcopy(dict(snapshot)))
    source = validated["source"]
    fresh = freshness(source, evaluated_at)
    reasons: set[str] = {"readonly_projection_only", "dispatch_dependency_missing"}
    if source["status"] == "unavailable":
        reasons.add("source_unavailable")
    elif source["status"] == "invalid":
        reasons.add("source_invalid")
    elif source["status"] == "ambiguous":
        reasons.add("authoritative_plan_ambiguous")
    if fresh == "stale":
        reasons.add("source_stale")
    elif fresh == "dirty":
        reasons.add("source_dirty")

    occ = occupancy(validated)
    remaining = validated["capacity"]["effective_writer_wip"] - occ
    work_items = {bucket: [] for bucket in ("ready", "waiting", "review", "active")}
    unclassified_ready = False
    ready_writer = 0
    for item in sorted(validated["work"], key=lambda value: value["source_id"]):
        item_reasons = list(item["reason_codes"])
        waiting_on = tuple(sorted(item["waiting_on"]))
        if item["state"] == "ready":
            ready_writer += 1 if item["phase"] == "writer" else 0
            if item["sensitivity"] == "unclassified" and source["status"] == "available" and fresh == "fresh":
                unclassified_ready = True
            if remaining <= 0 and item["phase"] == "writer":
                item_reasons.append("writer_wip_full")
        if item["state"] == "waiting":
            item_reasons.append("dependency_waiting")
        if item["state"] == "review" and not any(
            other["source_kind"] == "review_packet" for other in validated["work"]
        ):
            if any(
                agent.get("observed_state") == "done" or "review_authority_unavailable" in (agent.get("reason_codes") or [])
                for agent in validated["agents"]
            ):
                reasons.add("review_authority_unavailable")
        record = WorkProjectionItem(
            source_id=item["source_id"],
            source_kind=item["source_kind"],
            state=item["state"],
            waiting_on=waiting_on,
            reason_codes=tuple(sorted(set(item_reasons))),
        )
        if item["state"] in work_items:
            work_items[item["state"]].append(record)

    if unclassified_ready:
        reasons.add("sensitive_route_unavailable")

    agent_states: list[AgentProjectionItem] = []
    reason_counts: dict[str, int] = {}
    for agent in validated["agents"]:
        values = _agent_reasons(agent, evaluated_at)
        if any(item["sensitivity"] == "sensitive_security" for item in validated["work"]):
            if agent.get("verified_harness") not in SENSITIVE_OK:
                values.append("agent_policy_denied")
                reasons.add("agent_policy_denied")
                reasons.add("sensitive_route_unavailable")
        for item in validated["work"]:
            if item["phase"] == "reviewer" and item.get("author_agent_instance_id") == agent.get("agent_instance_id"):
                values.append("author_gate_denied")
                reasons.add("author_gate_denied")
        if not agent.get("identity_revision"):
            reasons.add("no_stable_identity")
        if agent.get("runtime_generation") is None:
            reasons.add("runtime_generation_unavailable")
        if "transport_unknown" in values:
            reasons.add("transport_unknown")
        if "review_authority_unavailable" in (agent.get("reason_codes") or []):
            reasons.add("review_authority_unavailable")
        projected = "unavailable"
        if (
            agent.get("identity_revision")
            and agent.get("runtime_generation") is not None
            and agent.get("attachment_id")
            and "source_stale" not in values
            and "transport_unknown" not in values
        ):
            if agent.get("observed_state") == "idle":
                projected = "available"
            elif agent.get("observed_state") in {"working", "blocked"}:
                projected = agent["observed_state"]
        if agent.get("projected_state") == "unknown_transport":
            projected = "unknown_transport"
        values = sorted(set(values))
        for code in values:
            reason_counts[code] = reason_counts.get(code, 0) + 1
        agent_states.append(AgentProjectionItem(
            agent_instance_id=agent.get("agent_instance_id"),
            projected_state=projected,
            reason_codes=tuple(values),
        ))
    agent_states.sort(key=lambda item: (item.agent_instance_id is None, item.agent_instance_id or ""))

    unlinked = False
    work_ids = {item["source_id"] for item in validated["work"]}
    for assignment in validated["assignments"]:
        linked = {link["id"] for link in assignment["typed_links"]}
        if not linked.intersection(work_ids):
            unlinked = True
            reasons.add("unlinked_authorities")

    pairs = eligible_pairs(validated, evaluated_at)
    ready_ids = [item.source_id for item in work_items["ready"]]
    if ready_ids and not pairs and fresh == "fresh" and source["status"] == "available":
        if any(item["sensitivity"] == "sensitive_security" for item in validated["work"] if item["state"] == "ready"):
            reasons.add("ready_but_no_eligible_agent")
            reasons.add("sensitive_route_unavailable")
        elif not validated["agents"]:
            reasons.add("no_stable_identity")

    alerts: list[SchedulerAlertIntent] = []
    first_observed = _first_observed(previous)
    if pairs and first_observed is not None and fresh == "fresh" and remaining > 0:
        held = int((evaluated_at - first_observed).total_seconds())
        if (evaluated_at - first_observed).total_seconds() >= READY_WITHOUT_DISPATCH_GRACE_SECONDS:
            alerts.append(SchedulerAlertIntent(
                kind="ready_without_dispatch", severity="warning",
                dedupe_key="ready_agent_idle_undispatched", status="open",
                first_observed_at=first_observed, observed_for_seconds=held,
                reason_code="ready_agent_idle_undispatched", evidence_refs=(),
            ))
            reasons.add("ready_agent_idle_undispatched")
        else:
            alerts.append(SchedulerAlertIntent(
                kind="ready_without_dispatch", severity="warning",
                dedupe_key="ready_agent_idle_undispatched", status="pending",
                first_observed_at=first_observed, observed_for_seconds=held,
                reason_code="ready_agent_idle_undispatched", evidence_refs=(),
            ))
    elif ready_ids and not pairs and source["status"] == "available" and fresh == "fresh":
        if any(item["sensitivity"] == "sensitive_security" for item in validated["work"] if item["state"] == "ready"):
            alerts.append(SchedulerAlertIntent(
                kind="ready_but_no_eligible_agent", severity="info",
                dedupe_key="ready_but_no_eligible_agent", status="absent",
                first_observed_at=None, observed_for_seconds=0,
                reason_code="ready_but_no_eligible_agent", evidence_refs=(),
            ))
    if previous is None:
        reasons.add("timing_unavailable")
        if not alerts:
            alerts.append(SchedulerAlertIntent(
                kind="ready_without_dispatch", severity="info",
                dedupe_key="timing_unavailable", status="absent",
                first_observed_at=None, observed_for_seconds=0,
                reason_code="timing_unavailable", evidence_refs=(),
            ))

    if any(agent.get("observed_state") == "done" for agent in validated["agents"]):
        if not any(item["source_kind"] == "review_packet" for item in validated["work"]):
            reasons.add("review_authority_unavailable")

    observed = _parse_utc(source["observed_at"])
    age = None
    if observed is not None:
        age = int((evaluated_at - observed).total_seconds())
    source_reasons = []
    if "source_unavailable" in reasons:
        source_reasons.append("source_unavailable")
    if "source_invalid" in reasons:
        source_reasons.append("source_invalid")
    if "authoritative_plan_ambiguous" in reasons:
        source_reasons.append("authoritative_plan_ambiguous")
    if "source_stale" in reasons:
        source_reasons.append("source_stale")
    if "source_dirty" in reasons:
        source_reasons.append("source_dirty")

    alerts.sort(key=lambda item: (item.kind, item.dedupe_key))
    return SchedulerProjection(
        schema_version=1,
        project_id=validated["project_id"],
        input_fingerprint=input_fingerprint(validated),
        evaluated_at=evaluated_at,
        next_reconciliation_at=evaluated_at + timedelta(seconds=RECONCILIATION_INTERVAL_SECONDS),
        source=SourceProjection(
            status=source["status"],
            freshness=fresh,
            observed_at=observed,
            age_seconds=age,
            revision=source["revision"],
            plan_sha256=source["plan_sha256"],
            reason_codes=tuple(sorted(source_reasons)),
        ),
        capacity=CapacityProjection(
            effective=validated["capacity"]["effective_writer_wip"],
            active=occ,
            remaining=remaining,
        ),
        work=WorkProjection(
            ready=tuple(work_items["ready"]),
            waiting=tuple(work_items["waiting"]),
            review=tuple(work_items["review"]),
            active=tuple(work_items["active"]),
        ),
        agents=AgentProjection(
            available_count=sum(1 for item in agent_states if item.projected_state == "available"),
            states=tuple(agent_states),
            reason_counts=tuple(sorted(reason_counts.items())),
        ),
        dispatch=DispatchProjection(
            available=False,
            mode="readonly",
            reason_code="readonly_projection_only",
            missing_dependencies=DISPATCH_DEPENDENCIES,
        ),
        alerts=tuple(alerts),
        reason_codes=tuple(sorted(reasons)),
    )
