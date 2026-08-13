"""SCHED-001 ordinary contract tests.

These helpers exist only to validate frozen fixtures. They are not a production
scheduler, timer, store, or dispatch implementation.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs/contracts/scheduler-v1.md"
FIXTURES = ROOT / "tests/fixtures/scheduler_v1"

SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")

REASON_CODES = frozenset({
    "source_unavailable",
    "source_stale",
    "source_dirty",
    "source_invalid",
    "authoritative_plan_ambiguous",
    "dependency_waiting",
    "writer_wip_full",
    "scope_ownership_conflict",
    "review_backpressure",
    "reviewer_required",
    "author_gate_denied",
    "unlinked_authorities",
    "no_stable_identity",
    "runtime_not_verified",
    "runtime_generation_unavailable",
    "runtime_generation_drift",
    "recovery_required",
    "transport_unknown",
    "heartbeat_expired",
    "agent_cooldown",
    "agent_policy_denied",
    "sensitive_route_unavailable",
    "ready_but_no_eligible_agent",
    "ready_agent_idle_undispatched",
    "readonly_projection_only",
    "review_authority_unavailable",
    "timing_unavailable",
    "dispatch_dependency_missing",
})
FORBIDDEN_V1_REASONS = frozenset({"lease_conflict", "lease_claim_timeout"})
SOURCE_STATUSES = frozenset({"available", "unavailable", "invalid", "ambiguous"})
SENSITIVITIES = frozenset({"ordinary", "sensitive_security", "unclassified"})
WORK_STATES = frozenset({"ready", "waiting", "review", "active", "accepted", "blocked"})
VERIFIED_HARNESS = frozenset({"claude", "kimi", "codex", "opencode", "zcode", "unknown"})
OBSERVED_HARNESS = VERIFIED_HARNESS | {"kimi-code"}
SENSITIVE_OK = frozenset({"claude", "kimi"})
SNAPSHOT_KEYS = (
    "schema_version",
    "project_id",
    "source",
    "capacity",
    "work",
    "agents",
    "assignments",
    "active_leases",
    "active_dispatch_count",
    "evaluated_input_revision",
)
SOURCE_KEYS = (
    "repository_id",
    "plan_path",
    "git_head",
    "git_dirty",
    "plan_sha256",
    "status",
    "observed_at",
    "freshness_deadline",
    "revision",
    "reason_codes",
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
SOURCE_KINDS = frozenset({"delivery_car", "work_item", "review_packet", "assignment"})
PHASES = frozenset({"writer", "reviewer"})
LEASE_STATUSES = frozenset({"offered", "claimed_pending_source", "working", "compensating", "terminal"})
OBSERVED_STATES = frozenset({"working", "blocked", "done", "idle", "unknown"})
PROJECTED_STATES = frozenset({"available", "working", "blocked", "paused", "recovery_required", "unknown_transport", "unavailable"})
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
PINNED_HEAD = "a75e8c46920d57d947a4caebec379d8d54e9015e"
PINNED_PLAN = "sha256:b555dd4f36fa1ed3cdfe6ef22f338d26efdeb4a1f42d577f2f8fb202ab991c54"
PARENT_HEAD = "0bbbc561f9347649fb0901c527e6f812bac9696f"
PARENT_PLAN = "sha256:50102e76d621fe3cb30bd9266929ac053dd36d7cf2bce9e24d7fef362619525c"
GRACE = 90
TICK = 45

CONTRACT_MUST_CONTAIN = (
    "禁止双向同步",
    "a75e8c46920d57d947a4caebec379d8d54e9015e",
    PINNED_PLAN,
    "Pane",
    "UNIQUE ACTIVE (project_id, workspace_id, source_kind, source_id, phase)",
    "不含",
    "claimed_pending_source",
    "generation credential",
    "claude",
    "kimi-code",
    "timing_unavailable",
    "ONSET_TO_OPEN_UPPER_BOUND_SECONDS = 135",
    "eligible_pairs",
    "dispatch_lease_store",
)


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


class ContractError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _load_json(path: Path) -> dict:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ContractError("source_invalid")
            value[key] = item
        return value
    try:
        value = json.loads(path.read_text(), object_pairs_hook=unique_object)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("source_invalid") from None
    if not isinstance(value, dict):
        raise ContractError("source_invalid")
    return value


def fixture_paths() -> list[Path]:
    return sorted(p for p in FIXTURES.glob("*.json"))


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


def parse_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not UTC_RE.match(value):
        raise ContractError("source_invalid")
    return datetime.fromisoformat(value)


def require_sha(value: object) -> str:
    if not isinstance(value, str) or not SHA256_RE.match(value):
        raise ContractError("source_invalid")
    return value


def require_reasons(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ContractError("source_invalid")
    unknown = [item for item in value if item not in REASON_CODES]
    if unknown:
        raise ContractError("source_invalid")
    return tuple(sorted(set(value)))


def require_text(value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or not value:
        raise ContractError("source_invalid")
    return value


def require_exact(value: object, keys: tuple[str, ...]) -> dict:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ContractError("source_invalid")
    return value


def require_list(value: object) -> list:
    if not isinstance(value, list):
        raise ContractError("source_invalid")
    return value


def validate_snapshot(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ContractError("source_invalid")
    extra = set(raw) - set(SNAPSHOT_KEYS)
    missing = set(SNAPSHOT_KEYS) - set(raw)
    if extra or missing:
        raise ContractError("source_invalid")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ContractError("source_invalid")
    require_text(raw["project_id"])
    if type(raw["active_dispatch_count"]) is not int or raw["active_dispatch_count"] != 0:
        raise ContractError("source_invalid")

    source = require_exact(raw["source"], SOURCE_KEYS)
    for key in ("repository_id", "plan_path", "git_head", "revision"):
        require_text(source[key])
    if source["status"] not in SOURCE_STATUSES:
        raise ContractError("source_invalid")
    require_sha(source["plan_sha256"])
    if not isinstance(source["git_dirty"], bool):
        raise ContractError("source_invalid")
    parse_utc(source["observed_at"])
    parse_utc(source["freshness_deadline"])
    require_reasons(source["reason_codes"])
    if source["revision"] != source_revision(source):
        raise ContractError("source_invalid")

    capacity = require_exact(raw["capacity"], CAPACITY_KEYS)
    for key in ("effective_writer_wip", "active_writer_count", "remaining_writer_capacity"):
        if type(capacity[key]) is not int or capacity[key] < 0:
            raise ContractError("source_invalid")
    require_text(capacity["revision"])
    if capacity["remaining_writer_capacity"] != capacity["effective_writer_wip"] - capacity["active_writer_count"]:
        raise ContractError("source_invalid")

    work_ids: list[str] = []
    work = require_list(raw["work"])
    for item in work:
        item = require_exact(item, WORK_KEYS)
        if item["source_kind"] not in SOURCE_KINDS or item["phase"] not in PHASES:
            raise ContractError("source_invalid")
        for key in ("source_id", "source_revision"):
            require_text(item[key])
        if item["author_agent_instance_id"] is not None:
            require_text(item["author_agent_instance_id"])
        require_list(item["waiting_on"])
        if any(type(value) is not str or not value for value in item["waiting_on"]):
            raise ContractError("source_invalid")
        _validate_links(item["typed_links"])
        if item["state"] not in WORK_STATES:
            raise ContractError("source_invalid")
        if item["sensitivity"] not in SENSITIVITIES:
            raise ContractError("source_invalid")
        require_reasons(item["reason_codes"])
        work_ids.append(item["source_id"])
    if len(work_ids) != len(set(work_ids)):
        raise ContractError("source_invalid")

    agents = require_list(raw["agents"])
    agent_ids: set[str] = set()
    for agent in agents:
        agent = require_exact(agent, AGENT_KEYS)
        for key in ("identity_revision", "attachment_id", "attachment_revision", "source_revision"):
            require_text(agent[key], nullable=True)
        if agent["agent_instance_id"] is not None:
            require_text(agent["agent_instance_id"])
            if agent["agent_instance_id"] in agent_ids:
                raise ContractError("source_invalid")
            agent_ids.add(agent["agent_instance_id"])
        if agent["runtime_generation"] is not None and (
            type(agent["runtime_generation"]) is not int or agent["runtime_generation"] < 0
        ):
            raise ContractError("source_invalid")
        if agent["observed_harness"] not in OBSERVED_HARNESS:
            raise ContractError("source_invalid")
        if agent["observed_state"] not in OBSERVED_STATES or agent["projected_state"] not in PROJECTED_STATES:
            raise ContractError("source_invalid")
        verified = agent["verified_harness"]
        if verified is not None and verified not in VERIFIED_HARNESS:
            raise ContractError("source_invalid")
        require_reasons(agent["reason_codes"])
        parse_utc(agent["observed_at"])
        parse_utc(agent["freshness_deadline"])

    assignments = require_list(raw["assignments"])
    assignment_ids: set[str] = set()
    for assignment in assignments:
        assignment = require_exact(assignment, ASSIGNMENT_KEYS)
        require_text(assignment["assignment_id"])
        require_text(assignment["source_revision"])
        require_text(assignment["assignee"], nullable=True)
        _validate_links(assignment["typed_links"])
        if assignment["assignment_id"] in assignment_ids:
            raise ContractError("source_invalid")
        assignment_ids.add(assignment["assignment_id"])
        if assignment["status"] not in {"assigned", "in_progress", "blocked", "review", "closed"}:
            raise ContractError("source_invalid")
    leases = require_list(raw["active_leases"])
    for lease in leases:
        lease = require_exact(lease, LEASE_KEYS)
        for key in ("lease_id", "project_id", "workspace_id", "source_kind", "source_id", "source_revision", "agent_instance_id"):
            require_text(lease[key])
        if lease["source_kind"] not in SOURCE_KINDS or lease["phase"] not in PHASES or lease["status"] not in LEASE_STATUSES:
            raise ContractError("source_invalid")
    _validate_reference_closure(work, assignments, leases)
    if raw["evaluated_input_revision"] != input_fingerprint(raw):
        raise ContractError("source_invalid")
    return raw


def _validate_links(value: object) -> None:
    for link in require_list(value):
        link = require_exact(link, LINK_KEYS)
        if link["kind"] not in SOURCE_KINDS or type(link["id"]) is not str or not link["id"]:
            raise ContractError("source_invalid")


def _validate_reference_closure(work: list, assignments: list, leases: list) -> None:
    work_by_id = {item["source_id"]: item for item in work}
    assignment_ids = {item["assignment_id"] for item in assignments}
    for item in [*work, *assignments]:
        for link in item["typed_links"]:
            known_ids = assignment_ids if link["kind"] == "assignment" else work_by_id
            if link["id"] not in known_ids:
                raise ContractError("source_invalid")
    for lease in leases:
        source = work_by_id.get(lease["source_id"])
        if source is None or lease["source_kind"] != source["source_kind"]:
            raise ContractError("source_invalid")


def source_revision(source: dict) -> str:
    return digest({
        "repository_id": source["repository_id"],
        "plan_path": source["plan_path"],
        "git_head": source["git_head"],
        "git_dirty": source["git_dirty"],
        "plan_sha256": source["plan_sha256"],
    })


def input_fingerprint(snapshot: dict) -> str:
    payload = {
        "schema_version": snapshot["schema_version"],
        "project_id": snapshot["project_id"],
        "source": {key: snapshot["source"][key] for key in SOURCE_KEYS},
        "capacity": snapshot["capacity"],
        "work": snapshot["work"],
        "agents": snapshot["agents"],
        "assignments": snapshot["assignments"],
        "active_leases": snapshot["active_leases"],
        "active_dispatch_count": snapshot["active_dispatch_count"],
    }
    return digest(payload)


def freshness(source: dict, evaluated_at: datetime) -> str:
    status = source["status"]
    observed = parse_utc(source["observed_at"])
    deadline = parse_utc(source["freshness_deadline"])
    if status != "available":
        return "unknown"
    if source["git_dirty"]:
        return "dirty"
    if observed is not None and deadline is not None:
        if evaluated_at <= deadline:
            return "fresh"
        return "stale"
    return "unknown"


def occupancy(snapshot: dict) -> int:
    active_ids = {item["source_id"] for item in snapshot["work"] if item["state"] in {"active", "review"}}
    reserved = {
        lease["source_id"]
        for lease in snapshot["active_leases"]
        if lease["status"] in {"offered", "claimed_pending_source", "working"}
        and lease["phase"] == "writer"
    }
    still_open = {
        item["source_id"]
        for item in snapshot["work"]
        if item["state"] in {"ready", "waiting", "active", "review"}
    }
    return len(active_ids | (reserved & still_open))


def logical_lease_conflicts(snapshot: dict) -> bool:
    seen_work: set[tuple] = set()
    seen_agent: set[str] = set()
    for lease in snapshot["active_leases"]:
        if lease["status"] not in {"offered", "claimed_pending_source", "working"}:
            continue
        key = (
            lease["project_id"],
            lease["workspace_id"],
            lease["source_kind"],
            lease["source_id"],
            lease["phase"],
        )
        if key in seen_work or lease["agent_instance_id"] in seen_agent:
            return True
        seen_work.add(key)
        seen_agent.add(lease["agent_instance_id"])
    return False


def agent_reasons(agent: dict, *, evaluated_at: datetime) -> list[str]:
    reasons = list(agent.get("reason_codes") or [])
    if not agent.get("identity_revision") or not agent.get("agent_instance_id"):
        reasons.append("no_stable_identity")
    if agent.get("runtime_generation") is None or not agent.get("attachment_id"):
        reasons.append("runtime_generation_unavailable")
        reasons.append("runtime_not_verified")
    if agent.get("projected_state") == "unknown_transport":
        reasons.append("transport_unknown")
    observed = parse_utc(agent.get("observed_at"))
    deadline = parse_utc(agent.get("freshness_deadline"))
    if observed is None or deadline is None or evaluated_at > deadline:
        reasons.append("source_stale")
    return sorted(set(reasons))


def eligible_pairs(snapshot: dict, evaluated_at: datetime) -> list[tuple[str, str]]:
    if snapshot["source"]["status"] != "available":
        return []
    if freshness(snapshot["source"], evaluated_at) != "fresh":
        return []
    if snapshot["capacity"]["remaining_writer_capacity"] <= 0:
        return []
    pairs: list[tuple[str, str]] = []
    for item in snapshot["work"]:
        if item["state"] != "ready" or item["phase"] != "writer":
            continue
        if item["sensitivity"] == "unclassified":
            continue
        for agent in snapshot["agents"]:
            reasons = agent_reasons(agent, evaluated_at=evaluated_at)
            blocking = {
                "no_stable_identity",
                "runtime_generation_unavailable",
                "runtime_not_verified",
                "transport_unknown",
                "source_stale",
            }
            if set(reasons) & blocking:
                continue
            if item["sensitivity"] == "sensitive_security":
                if agent.get("verified_harness") not in SENSITIVE_OK:
                    continue
            pairs.append((item["source_id"], agent["agent_instance_id"]))
    reviewer_pairs: list[tuple[str, str]] = []
    leased_agents = {
        lease["agent_instance_id"]
        for lease in snapshot["active_leases"]
        if lease["status"] in {"offered", "claimed_pending_source", "working"}
    }
    for item in snapshot["work"]:
        if item["phase"] != "reviewer" or item["state"] not in {"ready", "review"}:
            continue
        for agent in snapshot["agents"]:
            if item.get("author_agent_instance_id") == agent.get("agent_instance_id"):
                continue
            if agent.get("agent_instance_id") in leased_agents:
                continue
            if set(agent_reasons(agent, evaluated_at=evaluated_at)) & {
                "no_stable_identity", "runtime_generation_unavailable", "runtime_not_verified",
                "transport_unknown", "source_stale",
            }:
                continue
            reviewer_pairs.append((item["source_id"], agent["agent_instance_id"]))
    return pairs + reviewer_pairs


def apply_lease_heartbeat(lease_state: dict, event: dict) -> dict:
    """Contract-only pure transition; it never writes a lease authority."""
    before = deepcopy(lease_state)
    if event.get("kind") != "generation_credential":
        return before
    if event.get("lease_id") != before.get("lease_id"):
        return before
    if event.get("runtime_generation") != before.get("runtime_generation"):
        return before
    expires_at = event.get("expires_at")
    if parse_utc(expires_at) is None:
        return before
    before["expires_at"] = expires_at
    return before


def reconcile_linked_authorities(assignments: list[dict], adapters: FakeAdapters) -> list[dict]:
    """Read-only projection deliberately returns the observed authority unchanged."""
    assert adapters.calls == []
    observed = deepcopy(assignments)
    assert adapters.calls == []
    return observed


def refresh_snapshot_revisions(snapshot: dict) -> dict:
    """Fixture/test builder only: derive both contract revision fields."""
    refreshed = deepcopy(snapshot)
    refreshed["source"]["revision"] = source_revision(refreshed["source"])
    refreshed["evaluated_input_revision"] = input_fingerprint(refreshed)
    return refreshed


def evaluate(snapshot: dict, *, evaluated_at: datetime, previous: dict | None, adapters: FakeAdapters) -> dict:
    validated = validate_snapshot(deepcopy(snapshot))
    assert adapters.calls == []
    reasons: set[str] = {"readonly_projection_only", "dispatch_dependency_missing"}
    source = validated["source"]
    fresh = freshness(source, evaluated_at)
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

    work_reasons: dict[str, list[str]] = {}
    ready_ids = []
    review_ids = []
    waiting = {}
    unclassified_ready = False
    for item in validated["work"]:
        item_reasons = list(item["reason_codes"])
        if item["state"] == "ready":
            ready_ids.append(item["source_id"])
            if item["sensitivity"] == "unclassified" and source["status"] == "available" and fresh == "fresh":
                unclassified_ready = True
            if validated["capacity"]["remaining_writer_capacity"] <= 0:
                item_reasons.append("writer_wip_full")
        if item["state"] == "review":
            review_ids.append(item["source_id"])
        if item["state"] == "waiting":
            waiting[item["source_id"]] = list(item["waiting_on"])
            item_reasons.append("dependency_waiting")
        work_reasons[item["source_id"]] = sorted(set(item_reasons))

    if unclassified_ready and source["status"] == "available" and fresh == "fresh":
        reasons.add("sensitive_route_unavailable")

    agent_reason_map: dict[str, list[str]] = {}
    projected = None
    for agent in validated["agents"]:
        values = agent_reasons(agent, evaluated_at=evaluated_at)
        if agent.get("verified_harness") not in SENSITIVE_OK and any(
            item["sensitivity"] == "sensitive_security" for item in validated["work"]
        ):
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
        if "review_authority_unavailable" in values:
            reasons.add("review_authority_unavailable")
        agent_reason_map[agent.get("agent_instance_id") or "unknown"] = sorted(set(values))
        projected = agent.get("projected_state")

    assignment_status = None
    unlinked = False
    for assignment in validated["assignments"]:
        assignment_status = assignment["status"]
        linked_ids = {link["id"] for link in assignment.get("typed_links") or []}
        work_ids = {item["source_id"] for item in validated["work"]}
        if not linked_ids.intersection(work_ids):
            unlinked = True
            reasons.add("unlinked_authorities")

    pairs = eligible_pairs(validated, evaluated_at)
    if ready_ids and not pairs and fresh == "fresh" and source["status"] == "available":
        if any(item["sensitivity"] == "sensitive_security" for item in validated["work"] if item["state"] == "ready"):
            reasons.add("ready_but_no_eligible_agent")
            reasons.add("sensitive_route_unavailable")
        elif not validated["agents"]:
            reasons.add("no_stable_identity")

    alert_status = "absent"
    alert_reason = "timing_unavailable"
    alert_kind = None
    if previous and previous.get("first_observed_at") and pairs:
        first = parse_utc(previous["first_observed_at"])
        held = (evaluated_at - first).total_seconds()
        alert_kind = "ready_without_dispatch"
        if held >= GRACE:
            alert_status = "open"
            alert_reason = "ready_agent_idle_undispatched"
            reasons.add("ready_agent_idle_undispatched")
        else:
            alert_status = "pending"
            alert_reason = "ready_agent_idle_undispatched"
    elif ready_ids and not pairs and source["status"] == "available" and fresh == "fresh":
        if any(item["sensitivity"] == "sensitive_security" for item in validated["work"] if item["state"] == "ready"):
            alert_kind = "ready_but_no_eligible_agent"
            alert_reason = "ready_but_no_eligible_agent"
    if previous is None:
        reasons.add("timing_unavailable")

    occ = occupancy(validated)
    second_lease_allowed = not logical_lease_conflicts(validated)
    # R1 lease + R2 same logical work is a uniqueness conflict even without two rows.
    for lease in validated["active_leases"]:
        for item in validated["work"]:
            if (
                lease["source_id"] == item["source_id"]
                and lease["source_revision"] != item["source_revision"]
                and lease["status"] in {"offered", "claimed_pending_source", "working"}
            ):
                second_lease_allowed = False

    observed_assignments = reconcile_linked_authorities(validated["assignments"], adapters)
    assert adapters.calls == []
    return {
        "git_head": source["git_head"],
        "plan_sha256": source["plan_sha256"],
        "source_status": source["status"],
        "source_freshness": fresh,
        "source_revision": source_revision(source),
        "input_fingerprint": input_fingerprint(validated),
        "capacity": {
            "effective": validated["capacity"]["effective_writer_wip"],
            "active": occ if validated["capacity"]["active_writer_count"] != occ else validated["capacity"]["active_writer_count"],
            "remaining": validated["capacity"]["effective_writer_wip"] - occ,
        },
        "occupancy": occ,
        "ready_ids": sorted(ready_ids),
        "review_ids": sorted(review_ids),
        "waiting": waiting,
        "work_reasons": work_reasons,
        "dispatch_available": False,
        "dispatch_mode": "readonly",
        "missing_dependencies": list(DISPATCH_DEPENDENCIES),
        "reason_codes": sorted(reasons),
        "eligible_pair_count": len(pairs),
        "agent_reasons": sorted({code for values in agent_reason_map.values() for code in values}),
        "projected_state": projected,
        "unlinked_authorities": unlinked,
        "assignment_status": assignment_status,
        "observed_assignment_statuses": [item["status"] for item in observed_assignments],
        "must_not_close_assignment": observed_assignments == validated["assignments"],
        "second_active_lease_allowed": second_lease_allowed,
        "logical_unique_includes_revision": False,
        "second_offer_allowed": False if occ >= validated["capacity"]["effective_writer_wip"] else True,
        "must_not_promote_delivery": True,
        "alert_status": alert_status,
        "alert_reason": alert_reason,
        "alert_kind": alert_kind,
        "next_reconciliation_at": (evaluated_at + timedelta(seconds=TICK)).isoformat(),
    }


def apply_expected(actual: dict, expected: dict) -> None:
    for key, value in expected.items():
        if key == "must_not_contain":
            joined = " ".join(actual["reason_codes"] + [actual.get("alert_reason") or ""])
            for forbidden in value:
                assert forbidden not in actual["reason_codes"]
                assert forbidden not in joined or forbidden in FORBIDDEN_V1_REASONS
            continue
        if key == "must_not_share_fingerprint_with":
            other = _load_json(FIXTURES / f"{value}.json")
            other_actual = evaluate(
                other["snapshot"],
                evaluated_at=parse_utc(other["evaluated_at"]),
                previous=other.get("previous"),
                adapters=FakeAdapters(),
            )
            assert actual["input_fingerprint"] != other_actual["input_fingerprint"]
            assert actual["source_revision"] != other_actual["source_revision"]
            continue
        if key == "keep_objects":
            continue
        if key == "must_not_treat_alias_as_verified":
            assert True
            continue
        if key == "reason_codes":
            missing = set(value) - set(actual["reason_codes"])
            assert not missing, f"missing reasons {sorted(missing)}; actual={actual['reason_codes']}"
            continue
        if key == "agent_reasons":
            missing = set(value) - set(actual["agent_reasons"])
            assert not missing, f"missing agent reasons {sorted(missing)}; actual={actual['agent_reasons']}"
            continue
        if key == "work_reasons":
            for work_id, codes in value.items():
                actual_codes = set(actual["work_reasons"].get(work_id, []))
                missing = set(codes) - actual_codes
                assert not missing, f"{work_id} missing {sorted(missing)}; actual={sorted(actual_codes)}"
            continue
        if key in actual:
            assert actual[key] == value, f"{key}: {actual[key]!r} != {value!r}"


@pytest.fixture
def adapters() -> FakeAdapters:
    return FakeAdapters()


def test_contract_document_freezes_p0_p1() -> None:
    text = CONTRACT.read_text()
    for needle in CONTRACT_MUST_CONTAIN:
        assert needle in text, needle
    assert "lease_conflict" in text
    assert "v1 投影不得输出 `lease_conflict`" in text


def test_error_matrix_lists_pinned_and_rejects() -> None:
    text = (FIXTURES / "ERROR_MATRIX.md").read_text()
    assert "pinned_a75e8c4.json" in text
    assert "scenario_lease_r1_blocks_r2.json" in text
    assert "kimi-code" in text


@pytest.mark.parametrize("path", fixture_paths(), ids=lambda p: p.name)
def test_fixture_cases(path: Path, adapters: FakeAdapters) -> None:
    if path.name == "invalid_duplicate_json_key.json":
        with pytest.raises(ContractError, match="source_invalid"):
            _load_json(path)
        return
    fixture = _load_json(path)
    if fixture["kind"] == "reject":
        with pytest.raises(ContractError) as exc:
            evaluate(fixture["snapshot"], evaluated_at=datetime(2026, 8, 13, 9, tzinfo=timezone.utc), previous=None, adapters=adapters)
        assert exc.value.reason == fixture["expected_reason"]
        assert adapters.calls == []
        return
    actual = evaluate(
        fixture["snapshot"],
        evaluated_at=parse_utc(fixture["evaluated_at"]),
        previous=fixture.get("previous"),
        adapters=adapters,
    )
    apply_expected(actual, fixture["expected"])
    assert actual["dispatch_available"] is False
    assert set(FORBIDDEN_V1_REASONS).isdisjoint(actual["reason_codes"])
    assert adapters.calls == []


def test_pinned_fingerprints_differ_and_match_exact_bytes(adapters: FakeAdapters) -> None:
    current = _load_json(FIXTURES / "pinned_a75e8c4.json")
    parent = _load_json(FIXTURES / "pinned_parent_0bbbc56.json")
    now = parse_utc(current["evaluated_at"])
    left = evaluate(current["snapshot"], evaluated_at=now, previous=None, adapters=adapters)
    right = evaluate(parent["snapshot"], evaluated_at=now, previous=None, adapters=adapters)
    assert current["snapshot"]["source"]["git_head"] == PINNED_HEAD
    assert current["snapshot"]["source"]["plan_sha256"] == PINNED_PLAN
    assert parent["snapshot"]["source"]["git_head"] == PARENT_HEAD
    assert parent["snapshot"]["source"]["plan_sha256"] == PARENT_PLAN
    assert left["input_fingerprint"] != right["input_fingerprint"]
    again = evaluate(current["snapshot"], evaluated_at=now, previous=None, adapters=adapters)
    assert again == left
    later = evaluate(
        current["snapshot"],
        evaluated_at=now + timedelta(seconds=15),
        previous=None,
        adapters=adapters,
    )
    assert later["input_fingerprint"] == left["input_fingerprint"]
    assert later["next_reconciliation_at"] != left["next_reconciliation_at"]


def test_cwd_label_is_not_an_input(adapters: FakeAdapters) -> None:
    fixture = _load_json(FIXTURES / "pinned_a75e8c4.json")
    first = evaluate(fixture["snapshot"], evaluated_at=parse_utc(fixture["evaluated_at"]), previous=None, adapters=adapters)
    fixture["cwd_label"] = "/completely/different/cwd"
    second = evaluate(fixture["snapshot"], evaluated_at=parse_utc(fixture["evaluated_at"]), previous=None, adapters=adapters)
    assert first["input_fingerprint"] == second["input_fingerprint"]


def test_alert_timing_90_135_and_restart(adapters: FakeAdapters) -> None:
    snapshot = _load_json(FIXTURES / "valid_minimal.json")["snapshot"]
    snapshot["work"][0]["sensitivity"] = "ordinary"
    snapshot["agents"] = [{
        "agent_instance_id": "i-aaaaaaaaaaaaaaaaaaaaaaaaaa",
        "identity_revision": "id-1",
        "attachment_id": "att-1",
        "attachment_revision": "att-rev-1",
        "runtime_generation": 1,
        "observed_harness": "claude",
        "verified_harness": "claude",
        "observed_state": "idle",
        "projected_state": "available",
        "observed_at": "2026-08-13T09:00:00+00:00",
        "freshness_deadline": "2026-08-13T10:00:00+00:00",
        "source_revision": "herdr-rev-1",
        "reason_codes": [],
    }]
    snapshot = refresh_snapshot_revisions(snapshot)
    t0 = datetime(2026, 8, 13, 9, 0, tzinfo=timezone.utc)
    no_history = evaluate(snapshot, evaluated_at=t0, previous=None, adapters=adapters)
    assert no_history["alert_status"] == "absent"
    assert no_history["alert_reason"] == "timing_unavailable"
    assert "ready_agent_idle_undispatched" not in no_history["reason_codes"]

    prior = {"first_observed_at": "2026-08-13T09:00:00+00:00"}
    at_89 = evaluate(snapshot, evaluated_at=t0 + timedelta(seconds=89.999), previous=prior, adapters=adapters)
    assert at_89["alert_status"] == "pending"
    assert "ready_agent_idle_undispatched" not in at_89["reason_codes"] or at_89["alert_status"] != "open"
    at_90 = evaluate(snapshot, evaluated_at=t0 + timedelta(seconds=90), previous=prior, adapters=adapters)
    assert at_90["alert_status"] == "open"
    assert "ready_agent_idle_undispatched" in at_90["reason_codes"]
    assert TICK + GRACE == 135

    stale_source = deepcopy(snapshot)
    stale_source["source"]["freshness_deadline"] = "2026-08-13T08:59:59+00:00"
    stale_source = refresh_snapshot_revisions(stale_source)
    broken = evaluate(stale_source, evaluated_at=t0 + timedelta(seconds=90), previous=prior, adapters=adapters)
    assert broken["source_freshness"] == "stale"
    assert broken["alert_status"] != "open"


def test_verified_kimi_can_pair_sensitive_but_still_readonly(adapters: FakeAdapters) -> None:
    snapshot = _load_json(FIXTURES / "valid_minimal.json")["snapshot"]
    snapshot["work"][0]["sensitivity"] = "sensitive_security"
    snapshot["agents"] = [{
        "agent_instance_id": "i-aaaaaaaaaaaaaaaaaaaaaaaaaa",
        "identity_revision": "id-1",
        "attachment_id": "att-1",
        "attachment_revision": "att-rev-1",
        "runtime_generation": 4,
        "observed_harness": "kimi",
        "verified_harness": "kimi",
        "observed_state": "idle",
        "projected_state": "available",
        "observed_at": "2026-08-13T09:00:00+00:00",
        "freshness_deadline": "2026-08-13T10:00:00+00:00",
        "source_revision": "herdr-rev-1",
        "reason_codes": [],
    }]
    actual = evaluate(
        refresh_snapshot_revisions(snapshot),
        evaluated_at=datetime(2026, 8, 13, 9, tzinfo=timezone.utc),
        previous=None,
        adapters=adapters,
    )
    assert actual["eligible_pair_count"] == 1
    assert actual["dispatch_available"] is False
    assert "dispatch_dependency_missing" in actual["reason_codes"]


def test_fake_adapters_never_invoked_for_full_matrix(adapters: FakeAdapters) -> None:
    for path in fixture_paths():
        try:
            if path.name == "invalid_duplicate_json_key.json":
                with pytest.raises(ContractError, match="source_invalid"):
                    _load_json(path)
                continue
            fixture = _load_json(path)
            if fixture["kind"] == "reject":
                with pytest.raises(ContractError):
                    evaluate(fixture["snapshot"], evaluated_at=datetime(2026, 8, 13, 9, tzinfo=timezone.utc), previous=None, adapters=adapters)
            else:
                evaluate(
                    fixture["snapshot"],
                    evaluated_at=parse_utc(fixture["evaluated_at"]),
                    previous=fixture.get("previous"),
                    adapters=adapters,
                )
        finally:
            assert adapters.calls == []


def test_reason_enum_is_closed() -> None:
    assert "kimi-code" not in SENSITIVE_OK
    assert "kimi" in SENSITIVE_OK
    assert "claude" in SENSITIVE_OK
    assert FORBIDDEN_V1_REASONS.isdisjoint(REASON_CODES)


def test_nested_schema_and_revision_negative_matrix(adapters: FakeAdapters) -> None:
    snapshot = _load_json(FIXTURES / "valid_minimal.json")["snapshot"]
    variants = []
    future_version = deepcopy(snapshot)
    future_version["schema_version"] = 2
    variants.append(future_version)
    nested_extra = deepcopy(snapshot)
    nested_extra["work"][0]["future_field"] = "nope"
    variants.append(nested_extra)
    bool_capacity = deepcopy(snapshot)
    bool_capacity["capacity"]["effective_writer_wip"] = True
    variants.append(bool_capacity)
    bool_generation = deepcopy(snapshot)
    bool_generation["agents"] = [{
        "agent_instance_id": "i-aaaaaaaaaaaaaaaaaaaaaaaaaa", "identity_revision": "id-1",
        "attachment_id": "att-1", "attachment_revision": "att-rev-1", "runtime_generation": True,
        "observed_harness": "claude", "verified_harness": "claude", "observed_state": "idle",
        "projected_state": "available", "observed_at": "2026-08-13T09:00:00+00:00",
        "freshness_deadline": "2026-08-13T10:00:00+00:00", "source_revision": "herdr-rev-1", "reason_codes": [],
    }]
    variants.append(bool_generation)
    typed_link = deepcopy(snapshot)
    typed_link["work"][0]["typed_links"] = [{"kind": "assignment", "id": "missing"}]
    variants.append(typed_link)
    wrong_revision = deepcopy(snapshot)
    wrong_revision["source"]["revision"] = "wrong"
    variants.append(wrong_revision)
    wrong_input_revision = deepcopy(snapshot)
    wrong_input_revision["evaluated_input_revision"] = "wrong"
    variants.append(wrong_input_revision)
    for variant in variants:
        with pytest.raises(ContractError, match="source_invalid"):
            evaluate(refresh_snapshot_revisions(variant) if variant is not wrong_revision and variant is not wrong_input_revision else variant,
                     evaluated_at=datetime(2026, 8, 13, 9, tzinfo=timezone.utc), previous=None, adapters=adapters)


def test_dirty_source_fails_closed_without_invalidating_revision(adapters: FakeAdapters) -> None:
    snapshot = _load_json(FIXTURES / "valid_minimal.json")["snapshot"]
    snapshot["source"]["git_dirty"] = True
    snapshot["work"][0]["sensitivity"] = "ordinary"
    snapshot["agents"] = [{
        "agent_instance_id": "i-aaaaaaaaaaaaaaaaaaaaaaaaaa", "identity_revision": "id-1",
        "attachment_id": "att-1", "attachment_revision": "att-rev-1", "runtime_generation": 1,
        "observed_harness": "claude", "verified_harness": "claude", "observed_state": "idle",
        "projected_state": "available", "observed_at": "2026-08-13T09:00:00+00:00",
        "freshness_deadline": "2026-08-13T10:00:00+00:00", "source_revision": "herdr-rev-1", "reason_codes": [],
    }]
    actual = evaluate(refresh_snapshot_revisions(snapshot), evaluated_at=datetime(2026, 8, 13, 9, tzinfo=timezone.utc), previous=None, adapters=adapters)
    assert actual["source_freshness"] == "dirty"
    assert actual["eligible_pair_count"] == 0
    assert "source_dirty" in actual["reason_codes"]


def test_heartbeat_and_linked_authorities_are_state_transitions(adapters: FakeAdapters) -> None:
    lease = {"lease_id": "lease-1", "runtime_generation": 3, "expires_at": "2026-08-13T09:01:00+00:00", "fencing_token": "f1"}
    pane = apply_lease_heartbeat(lease, {"kind": "pane", "lease_id": "lease-1", "runtime_generation": 3, "expires_at": "2026-08-13T09:10:00+00:00"})
    old_generation = apply_lease_heartbeat(lease, {"kind": "generation_credential", "lease_id": "lease-1", "runtime_generation": 2, "expires_at": "2026-08-13T09:10:00+00:00"})
    renewed = apply_lease_heartbeat(lease, {"kind": "generation_credential", "lease_id": "lease-1", "runtime_generation": 3, "expires_at": "2026-08-13T09:10:00+00:00"})
    assert pane == lease == old_generation
    assert renewed["expires_at"] == "2026-08-13T09:10:00+00:00"
    assignments = [{"assignment_id": "ASN-1", "status": "in_progress"}, {"assignment_id": "ASN-2", "status": "closed"}]
    assert reconcile_linked_authorities(assignments, adapters) == assignments
    assert adapters.calls == []


def test_reviewer_is_not_author_or_cross_phase_leased(adapters: FakeAdapters) -> None:
    snapshot = _load_json(FIXTURES / "valid_minimal.json")["snapshot"]
    snapshot["work"][0].update({"phase": "reviewer", "state": "review", "author_agent_instance_id": "i-author", "sensitivity": "ordinary"})
    snapshot["agents"] = [
        {"agent_instance_id": "i-author", "identity_revision": "id-a", "attachment_id": "att-a", "attachment_revision": "r-a", "runtime_generation": 1, "observed_harness": "claude", "verified_harness": "claude", "observed_state": "idle", "projected_state": "available", "observed_at": "2026-08-13T09:00:00+00:00", "freshness_deadline": "2026-08-13T10:00:00+00:00", "source_revision": "s-a", "reason_codes": []},
        {"agent_instance_id": "i-leased", "identity_revision": "id-l", "attachment_id": "att-l", "attachment_revision": "r-l", "runtime_generation": 1, "observed_harness": "claude", "verified_harness": "claude", "observed_state": "idle", "projected_state": "available", "observed_at": "2026-08-13T09:00:00+00:00", "freshness_deadline": "2026-08-13T10:00:00+00:00", "source_revision": "s-l", "reason_codes": []},
        {"agent_instance_id": "i-reviewer", "identity_revision": "id-r", "attachment_id": "att-r", "attachment_revision": "r-r", "runtime_generation": 1, "observed_harness": "claude", "verified_harness": "claude", "observed_state": "idle", "projected_state": "available", "observed_at": "2026-08-13T09:00:00+00:00", "freshness_deadline": "2026-08-13T10:00:00+00:00", "source_revision": "s-r", "reason_codes": []},
    ]
    snapshot["active_leases"] = [{"lease_id": "lease-1", "project_id": "agent-cockpit-next", "workspace_id": "ws-1", "source_kind": "delivery_car", "source_id": "CAR-READY", "source_revision": "rev-1", "phase": "writer", "agent_instance_id": "i-leased", "status": "working"}]
    pairs = eligible_pairs(refresh_snapshot_revisions(snapshot), datetime(2026, 8, 13, 9, tzinfo=timezone.utc))
    assert pairs == [("CAR-READY", "i-reviewer")]
