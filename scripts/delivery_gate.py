#!/usr/bin/env python3
"""Read-only validator for versioned delivery plans."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from typing import NamedTuple
from pathlib import Path


STATUSES = {
    "planned", "in_progress", "review", "accepted", "releasing", "canary",
    "user_accepted", "blocked", "cancelled",
}
ACTIVE = {"in_progress", "review"}
SATISFIED = {"accepted", "user_accepted"}
SHA_STATUSES = {"review", "accepted", "releasing", "canary", "user_accepted"}
CAR_FIELDS = {
    "id", "title", "status", "depends_on", "scope", "acceptance", "rollback",
    "production_impact", "owner_instance_id", "reviewer_instance_id", "base_sha",
    "fixed_sha", "cross_module_block_count", "release_started_at",
    "user_acceptance_required", "user_acceptance_evidence",
}
ROOT_FIELDS = {
    "schema_version", "goal_id", "user_journey", "non_goals", "baseline",
    "limits", "cars",
}
BASELINE_FIELDS = {"main_sha", "production_version", "production_source_sha"}
LIMIT_FIELDS_V1 = {"writer_wip", "release_minutes", "cross_module_blocks_before_reslice"}
LIMIT_FIELDS_V2 = LIMIT_FIELDS_V1 | {"writer_wip_gates"}
WRITER_WIP_GATE_FIELDS = {"car_id", "from", "to"}
ACCEPTANCE_FIELDS = {"command", "passed"}
INSTANCE_ID_RE = re.compile(r"^i-[a-z2-7]{26}$")
WRITER_WIP_GATE_ID_RE = re.compile(r"^DELIVERY-[0-9]{3}-wip([0-9]+)-gate$")
WIP4_GATE_ID = "DELIVERY-003-wip4-gate"
PROVIDER_OWNERSHIP_PATH = ".delivery/provider-ownership-v1.json"
DELIVERY_PLAN_PATH = ".delivery/cockpit-product-v3.json"
PROVIDER_OWNERSHIP_FIELDS = {
    "schema_version", "gate_id", "transition", "global_hotspots", "partitions",
}
PROVIDER_TRANSITION_FIELDS = {"from", "to"}
PROVIDER_PARTITION_FIELDS = {
    "id", "car_id", "scopes", "store_migration_scope", "entrypoint_scope",
}
PROVIDER_PARTITIONS = (
    ("operation", "OPERATION-001-journal"),
    ("runtime_provider", "RUNTIME-002-provider"),
    ("event", "EVENT-001-journal"),
    ("memory", "MEMORY-001-store"),
)
GLOBAL_HOTSPOTS = (
    "agent_cockpit/runtime_paths.py",
    "agent_cockpit/server.py",
    "agent_cockpit/store_schema.py",
    "server.py",
)


class WriterWipEvaluation(NamedTuple):
    effective: int
    errors: tuple[dict, ...]


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(key)
        value[key] = item
    return value


def invalid_constant(value: str) -> None:
    raise json.JSONDecodeError("invalid constant", value, 0)


def issue(code: str, *, car_id: str | None = None, detail: str = "") -> dict:
    value = {"code": code}
    if car_id is not None:
        value["car_id"] = car_id
    if detail:
        value["detail"] = detail
    return value


def exact_fields(value: object, expected: set[str], code: str, errors: list[dict], **where) -> bool:
    if not isinstance(value, dict):
        errors.append(issue(code, detail="expected object", **where))
        return False
    missing = sorted(expected - value.keys())
    unknown = sorted(value.keys() - expected)
    if missing:
        errors.append(issue("missing_field", detail=",".join(missing), **where))
    if unknown:
        errors.append(issue("unknown_field", detail=",".join(unknown), **where))
    return not missing and not unknown


def strings(value: object, *, allow_empty: bool = False) -> bool:
    return isinstance(value, list) and (allow_empty or bool(value)) and all(
        isinstance(item, str) and bool(item.strip()) for item in value
    )


def is_sha(value: object) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(c in "0123456789abcdef" for c in value)


def is_instance(value: object) -> bool:
    return isinstance(value, str) and bool(INSTANCE_ID_RE.fullmatch(value))


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], text=True, capture_output=True, check=False,
    )


def repo_root(path: Path) -> Path:
    for root in (path.resolve().parent, Path.cwd()):
        result = git(root, "rev-parse", "--show-toplevel")
        if result.returncode == 0:
            return Path(result.stdout.strip())
    return path.resolve().parent


def parse_time(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else None
    except ValueError:
        return None


def scope_parts(value: str) -> tuple[str, ...]:
    return tuple(part for part in Path(value).parts if part not in {"", "."})


def scopes_overlap(left: str, right: str) -> bool:
    left_parts = scope_parts(left)
    right_parts = scope_parts(right)
    shared = min(len(left_parts), len(right_parts))
    return left_parts[:shared] == right_parts[:shared]


def _valid_scope_list(value: object) -> bool:
    return (
        strings(value)
        and value == sorted(value)
        and not any(
            Path(item).is_absolute() or ".." in Path(item).parts or not scope_parts(item)
            for item in value
        )
        and len({scope_parts(item) for item in value}) == len(value)
        and not any(
            scopes_overlap(left, right)
            for index, left in enumerate(value)
            for right in value[index + 1:]
        )
    )


def _valid_car_scopes(value: object) -> bool:
    return (
        strings(value)
        and not any(
            Path(item).is_absolute() or ".." in Path(item).parts or not scope_parts(item)
            for item in value
        )
        and len({scope_parts(item) for item in value}) == len(value)
        and not any(
            scopes_overlap(left, right)
            for index, left in enumerate(value)
            for right in value[index + 1:]
        )
    )


def _valid_scope_path(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and not Path(value).is_absolute()
        and ".." not in Path(value).parts
        and value == "/".join(Path(value).parts)
    )


def _git_json(repo: Path, revision: str, path: str) -> object:
    result = git(repo, "show", f"{revision}:{path}")
    if result.returncode:
        raise OSError(path)
    return json.loads(
        result.stdout, object_pairs_hook=unique_object, parse_constant=invalid_constant,
    )


def _provider_evidence(
    plan: dict, repo: Path | None, by_id: dict[str, dict], gate_car: dict,
) -> tuple[dict, ...]:
    required = issue("provider_ownership_evidence_required", car_id=WIP4_GATE_ID)
    if repo is None:
        return (required,)
    base_sha, fixed_sha = gate_car.get("base_sha"), gate_car.get("fixed_sha")
    if not is_sha(base_sha) or not is_sha(fixed_sha) or base_sha == fixed_sha:
        return (required,)
    if (
        git(repo, "cat-file", "-e", f"{base_sha}^{{commit}}").returncode
        or git(repo, "cat-file", "-e", f"{fixed_sha}^{{commit}}").returncode
        or git(repo, "merge-base", "--is-ancestor", base_sha, fixed_sha).returncode
    ):
        return (required,)
    fixed_blob = git(repo, "rev-parse", f"{fixed_sha}:{PROVIDER_OWNERSHIP_PATH}")
    if fixed_blob.returncode:
        return (required,)
    base_blob = git(repo, "rev-parse", f"{base_sha}:{PROVIDER_OWNERSHIP_PATH}")
    if base_blob.returncode == 0 and base_blob.stdout.strip() == fixed_blob.stdout.strip():
        return (required,)
    try:
        evidence = _git_json(repo, fixed_sha, PROVIDER_OWNERSHIP_PATH)
        fixed_plan = _git_json(repo, fixed_sha, DELIVERY_PLAN_PATH)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return (issue("invalid_provider_ownership_evidence", car_id=WIP4_GATE_ID),)
    invalid = issue("invalid_provider_ownership_evidence", car_id=WIP4_GATE_ID)
    if not isinstance(evidence, dict) or set(evidence) != PROVIDER_OWNERSHIP_FIELDS:
        return (invalid,)
    transition = evidence["transition"]
    partitions = evidence["partitions"]
    if (
        type(evidence["schema_version"]) is not int
        or evidence["schema_version"] != 1
        or evidence["gate_id"] != WIP4_GATE_ID
        or not isinstance(transition, dict)
        or set(transition) != PROVIDER_TRANSITION_FIELDS
        or type(transition["from"]) is not int
        or type(transition["to"]) is not int
        or transition != {"from": 3, "to": 4}
        or evidence["global_hotspots"] != list(GLOBAL_HOTSPOTS)
        or not isinstance(partitions, list)
        or len(partitions) != len(PROVIDER_PARTITIONS)
        or not isinstance(fixed_plan, dict)
        or not isinstance(fixed_plan.get("cars"), list)
    ):
        return (invalid,)
    fixed_ids: list[str] = []
    for car in fixed_plan["cars"]:
        if (
            not isinstance(car, dict)
            or not isinstance(car.get("id"), str)
            or not isinstance(car.get("status"), str)
            or car["status"] not in STATUSES
            or not strings(car.get("depends_on"), allow_empty=True)
            or not _valid_car_scopes(car.get("scope"))
        ):
            return (invalid,)
        fixed_ids.append(car["id"])
    if len(set(fixed_ids)) != len(fixed_ids):
        return (invalid,)
    fixed_by_id = {car["id"]: car for car in fixed_plan["cars"]}
    all_scopes: list[tuple[str, str]] = []
    migration_scopes: list[tuple[str, str]] = []
    entrypoint_scopes: list[tuple[str, str]] = []
    for partition, expected in zip(partitions, PROVIDER_PARTITIONS):
        if (
            not isinstance(partition, dict)
            or set(partition) != PROVIDER_PARTITION_FIELDS
            or (partition.get("id"), partition.get("car_id")) != expected
            or not _valid_scope_list(partition.get("scopes"))
            or not _valid_scope_path(partition.get("store_migration_scope"))
            or not _valid_scope_path(partition.get("entrypoint_scope"))
        ):
            return (invalid,)
        owner = partition["car_id"]
        all_scopes.extend((owner, scope) for scope in partition["scopes"])
        migration_scopes.append((owner, partition["store_migration_scope"]))
        entrypoint_scopes.append((owner, partition["entrypoint_scope"]))

    overlap = issue("provider_ownership_overlap", car_id=WIP4_GATE_ID)
    for values in (all_scopes, migration_scopes, entrypoint_scopes):
        if any(
            left_owner != right_owner and scopes_overlap(left, right)
            for index, (left_owner, left) in enumerate(values)
            for right_owner, right in values[index + 1:]
        ):
            return (overlap,)
    if any(
        scopes_overlap(scope, hotspot)
        for _owner, scope in all_scopes
        for hotspot in GLOBAL_HOTSPOTS
    ):
        return (overlap,)
    if any(
        not any(
            scopes_overlap(value, scope)
            and len(scope_parts(scope)) <= len(scope_parts(value))
            for scope in partition["scopes"]
        )
        for partition in partitions
        for value in (
            partition["store_migration_scope"], partition["entrypoint_scope"],
        )
    ):
        return (invalid,)

    mismatch = issue("provider_ownership_car_mismatch", car_id=WIP4_GATE_ID)
    provider_ids = {partition["car_id"] for partition in partitions}
    for partition in partitions:
        fixed_car = fixed_by_id.get(partition["car_id"])
        current_car = by_id.get(partition["car_id"])
        if (
            not isinstance(fixed_car, dict)
            or fixed_car.get("status") != "planned"
            or fixed_car.get("depends_on") != [WIP4_GATE_ID]
            or fixed_car.get("scope") != partition["scopes"]
            or not isinstance(current_car, dict)
            or current_car.get("depends_on") != [WIP4_GATE_ID]
            or current_car.get("scope") != partition["scopes"]
        ):
            return (mismatch,)

    for current_by_id in (fixed_by_id, by_id):
        runnable = [
            car for car in current_by_id.values()
            if car.get("id") not in provider_ids | {WIP4_GATE_ID}
            and (
                car.get("status") in ACTIVE
                or (
                    car.get("status") == "planned"
                    and all(
                        current_by_id.get(dependency, {}).get("status") in SATISFIED
                        for dependency in car.get("depends_on", [])
                    )
                )
            )
        ]
        if any(
            scopes_overlap(provider_scope, other_scope)
            for _owner, provider_scope in all_scopes
            for car in runnable
            for other_scope in car.get("scope", [])
        ):
            return (overlap,)
    return ()


def evaluate_writer_wip(
    plan: dict, repo: Path | None = None, by_id: dict[str, dict] | None = None,
) -> WriterWipEvaluation:
    limits = plan["limits"]
    effective = limits["writer_wip"]
    if plan["schema_version"] != 2:
        return WriterWipEvaluation(effective, ())
    cars = by_id if by_id is not None else {car["id"]: car for car in plan["cars"]}
    errors: tuple[dict, ...] = ()
    seen_gate_ids: set[str] = set()
    for gate in limits.get("writer_wip_gates", []):
        if not isinstance(gate, dict) or set(gate) != WRITER_WIP_GATE_FIELDS:
            break
        gate_id = gate["car_id"]
        match = WRITER_WIP_GATE_ID_RE.fullmatch(gate_id) if isinstance(gate_id, str) else None
        if (
            match is None
            or type(gate["from"]) is not int
            or type(gate["to"]) is not int
            or effective != gate["from"]
            or gate["to"] != gate["from"] + 1
            or match.group(1) != str(gate["to"])
            or gate_id in seen_gate_ids
            or (gate["to"] == 4 and gate["car_id"] != WIP4_GATE_ID)
        ):
            break
        seen_gate_ids.add(gate_id)
        car = cars.get(gate["car_id"])
        if not car:
            break
        if gate["car_id"] == WIP4_GATE_ID and car.get("status") in SHA_STATUSES:
            errors = _provider_evidence(plan, repo, cars, car)
        if car.get("status") not in SATISFIED or errors:
            break
        effective = gate["to"]
    return WriterWipEvaluation(effective, errors)


def effective_writer_wip(
    plan: dict, repo: Path | None = None, by_id: dict[str, dict] | None = None,
) -> int:
    return evaluate_writer_wip(plan, repo, by_id).effective


def load(path: Path) -> tuple[dict | None, list[dict]]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=unique_object,
            parse_constant=invalid_constant,
        )
    except json.JSONDecodeError as exc:
        return None, [issue("invalid_json", detail=str(exc))]
    except ValueError as exc:
        return None, [issue("duplicate_json_key", detail=str(exc))]
    except (OSError, UnicodeError) as exc:
        return None, [issue("invalid_json", detail=str(exc))]
    if not isinstance(value, dict):
        return None, [issue("invalid_root", detail="expected object")]
    return value, []


def validate(plan: dict, repo: Path, now: dt.datetime | None = None) -> list[dict]:
    errors: list[dict] = []
    if not exact_fields(plan, ROOT_FIELDS, "invalid_root", errors):
        return errors
    schema_version = plan["schema_version"]
    if type(schema_version) is not int or schema_version not in {1, 2}:
        errors.append(issue("unsupported_schema_version"))
    for field in ("goal_id", "user_journey"):
        if not isinstance(plan[field], str) or not plan[field].strip():
            errors.append(issue("invalid_field", detail=field))
    if not strings(plan["non_goals"]):
        errors.append(issue("invalid_field", detail="non_goals"))
    baseline = plan["baseline"]
    if not exact_fields(baseline, BASELINE_FIELDS, "invalid_baseline", errors):
        return errors
    if not is_sha(baseline["main_sha"]) or not is_sha(baseline["production_source_sha"]):
        errors.append(issue("invalid_baseline_sha"))
    if not isinstance(baseline["production_version"], str) or not baseline["production_version"].strip():
        errors.append(issue("invalid_field", detail="baseline.production_version"))
    limits = plan["limits"]
    limit_fields = LIMIT_FIELDS_V2 if schema_version == 2 else LIMIT_FIELDS_V1
    if not exact_fields(limits, limit_fields, "invalid_limits", errors):
        if schema_version == 2 and (
            not isinstance(limits, dict) or "writer_wip_gates" not in limits
        ):
            errors.append(issue("writer_wip_gate_required"))
        return errors
    expected = {"writer_wip": 2, "release_minutes": 15, "cross_module_blocks_before_reslice": 2}
    for field, value in expected.items():
        if type(limits[field]) is not int or limits[field] != value:
            errors.append(issue("invalid_limit", detail=f"{field}={limits[field]!r}"))
    if any(type(limits[field]) is not int or limits[field] != value for field, value in expected.items()):
        return errors
    writer_wip_gates: list[dict] = []
    if schema_version == 2:
        gates = limits["writer_wip_gates"]
        if not isinstance(gates, list) or not gates:
            errors.append(issue("writer_wip_gate_required"))
            return errors
        expected_from = limits["writer_wip"]
        seen_gate_ids: set[str] = set()
        for gate in gates:
            if not exact_fields(gate, WRITER_WIP_GATE_FIELDS, "invalid_writer_wip_gate", errors):
                errors.append(issue("writer_wip_gate_required"))
                continue
            car_id = gate["car_id"]
            match = WRITER_WIP_GATE_ID_RE.fullmatch(car_id) if isinstance(car_id, str) else None
            transition_valid = (
                type(gate["from"]) is int
                and type(gate["to"]) is int
                and gate["from"] == expected_from
                and gate["to"] == gate["from"] + 1
                and match is not None
                and match.group(1) == str(gate["to"])
                and (gate["to"] != 4 or car_id == WIP4_GATE_ID)
                and car_id not in seen_gate_ids
            )
            if not transition_valid:
                errors.append(issue("writer_wip_gate_required"))
                continue
            writer_wip_gates.append(gate)
            seen_gate_ids.add(car_id)
            expected_from = gate["to"]
    cars = plan["cars"]
    if not isinstance(cars, list) or not cars:
        errors.append(issue("invalid_cars"))
        return errors

    valid_cars: list[dict] = []
    ids: set[str] = set()
    for index, car in enumerate(cars):
        car_id = car.get("id") if isinstance(car, dict) and isinstance(car.get("id"), str) else f"#{index}"
        if not exact_fields(car, CAR_FIELDS, "invalid_car", errors, car_id=car_id):
            continue
        id_valid = bool(car_id) and not car_id.startswith("#") and not car_id.isspace()
        if not id_valid:
            errors.append(issue("invalid_car_id", car_id=car_id))
        elif car_id in ids:
            errors.append(issue("duplicate_car_id", car_id=car_id))
        ids.add(car_id)
        if not isinstance(car["title"], str) or not car["title"].strip():
            errors.append(issue("invalid_field", car_id=car_id, detail="title"))
        status_valid = isinstance(car["status"], str) and car["status"] in STATUSES
        if not status_valid:
            errors.append(issue("invalid_status", car_id=car_id))
        depends_valid = strings(car["depends_on"], allow_empty=True)
        if not depends_valid:
            errors.append(issue("invalid_depends_on", car_id=car_id))
        scope_valid = (
            strings(car["scope"])
            and not any(
                Path(p).is_absolute()
                or ".." in Path(p).parts
                or not scope_parts(p)
                for p in car["scope"]
            )
            and len({scope_parts(p) for p in car["scope"]}) == len(car["scope"])
            and not any(
                scopes_overlap(left, right)
                for index, left in enumerate(car["scope"])
                for right in car["scope"][index + 1:]
            )
        )
        if not scope_valid:
            errors.append(issue("invalid_scope", car_id=car_id))
        acceptance = car["acceptance"]
        acceptance_valid = isinstance(acceptance, list) and bool(acceptance)
        if not isinstance(acceptance, list) or not acceptance:
            errors.append(issue("missing_acceptance", car_id=car_id))
        else:
            for item in acceptance:
                if not exact_fields(item, ACCEPTANCE_FIELDS, "invalid_acceptance", errors, car_id=car_id):
                    acceptance_valid = False
                    continue
                if not isinstance(item["command"], str) or not item["command"].strip() or not isinstance(item["passed"], bool):
                    errors.append(issue("invalid_acceptance", car_id=car_id))
                    acceptance_valid = False
        if not isinstance(car["rollback"], str) or not car["rollback"].strip():
            errors.append(issue("missing_rollback", car_id=car_id))
        if not isinstance(car["production_impact"], str) or car["production_impact"] not in {"none", "dark", "canary", "release"}:
            errors.append(issue("invalid_production_impact", car_id=car_id))
        for field in ("base_sha", "fixed_sha", "release_started_at", "user_acceptance_evidence"):
            if car[field] is not None and (not isinstance(car[field], str) or not car[field].strip()):
                errors.append(issue("invalid_field", car_id=car_id, detail=field))
        for field in ("owner_instance_id", "reviewer_instance_id"):
            if car[field] is not None and not is_instance(car[field]):
                errors.append(issue("invalid_instance_id", car_id=car_id, detail=field))
        block_valid = type(car["cross_module_block_count"]) is int and car["cross_module_block_count"] >= 0
        if not block_valid:
            errors.append(issue("invalid_block_count", car_id=car_id))
        user_flag_valid = isinstance(car["user_acceptance_required"], bool)
        if not user_flag_valid:
            errors.append(issue("invalid_field", car_id=car_id, detail="user_acceptance_required"))
        if id_valid and status_valid and depends_valid and scope_valid and acceptance_valid and block_valid and user_flag_valid:
            valid_cars.append(car)

    by_id = {car["id"]: car for car in valid_cars if isinstance(car["id"], str)}
    for index, gate in enumerate(writer_wip_gates):
        car = by_id.get(gate["car_id"])
        if car is None:
            errors.append(issue("writer_wip_gate_required", detail=gate["car_id"]))
        elif index and writer_wip_gates[index - 1]["car_id"] not in car["depends_on"]:
            errors.append(issue("writer_wip_gate_required", car_id=gate["car_id"], detail="gate_chain"))
    for car in valid_cars:
        car_id, status = car["id"], car["status"]
        unknown = sorted(set(car["depends_on"]) - by_id.keys())
        if unknown:
            errors.append(issue("unknown_dependency", car_id=car_id, detail=",".join(unknown)))
        if status in ACTIVE | SHA_STATUSES and not is_instance(car["owner_instance_id"]):
            errors.append(issue("owner_required", car_id=car_id))
        if status in SHA_STATUSES:
            if not is_instance(car["reviewer_instance_id"]) or car["reviewer_instance_id"] == car["owner_instance_id"]:
                errors.append(issue("independent_reviewer_required", car_id=car_id))
            if not is_sha(car["base_sha"]) or not is_sha(car["fixed_sha"]):
                errors.append(issue("exact_sha_required", car_id=car_id))
            elif git(repo, "cat-file", "-e", f"{car['fixed_sha']}^{{commit}}").returncode:
                errors.append(issue("fixed_sha_not_found", car_id=car_id))
            else:
                changed = git(repo, "diff", "--name-only", car["base_sha"], car["fixed_sha"])
                if changed.returncode:
                    errors.append(issue("diff_unavailable", car_id=car_id))
                else:
                    outside = sorted(p for p in changed.stdout.splitlines() if not any(
                        scopes_overlap(p, scope) and len(scope_parts(scope)) <= len(scope_parts(p))
                        for scope in car["scope"]
                    ))
                    if outside:
                        errors.append(issue("scope_violation", car_id=car_id, detail=",".join(outside)))
        if status in SATISFIED | {"releasing", "canary"}:
            waiting = [dep for dep in car["depends_on"] if dep in by_id and by_id[dep]["status"] not in SATISFIED]
            if waiting:
                errors.append(issue("dependency_not_satisfied", car_id=car_id, detail=",".join(waiting)))
            if any(not item.get("passed", False) for item in car["acceptance"] if isinstance(item, dict)):
                errors.append(issue("acceptance_not_passed", car_id=car_id))
        if status in {"releasing", "canary"}:
            started = parse_time(car["release_started_at"])
            if not started:
                errors.append(issue("release_start_required", car_id=car_id))
            else:
                current = now or dt.datetime.now(dt.timezone.utc)
                elapsed = current.astimezone(dt.timezone.utc) - started.astimezone(dt.timezone.utc)
                if elapsed < dt.timedelta(0):
                    errors.append(issue("release_start_in_future", car_id=car_id))
                elif elapsed > dt.timedelta(minutes=limits["release_minutes"]):
                    errors.append(issue("release_timeout", car_id=car_id))
        if car["cross_module_block_count"] >= limits["cross_module_blocks_before_reslice"] and status not in {"blocked", "cancelled"}:
            errors.append(issue("reslice_required", car_id=car_id))
        if status == "user_accepted":
            errors.append(issue("user_acceptance_evidence_required", car_id=car_id))

    indegree = {car_id: 0 for car_id in by_id}
    followers = {car_id: [] for car_id in by_id}
    for car_id, car in by_id.items():
        for dependency in car["depends_on"]:
            if dependency in by_id:
                indegree[car_id] += 1
                followers[dependency].append(car_id)
    pending = [car_id for car_id, count in indegree.items() if count == 0]
    order: list[str] = []
    visited = 0
    while pending:
        current = pending.pop()
        order.append(current)
        visited += 1
        for follower in followers[current]:
            indegree[follower] -= 1
            if indegree[follower] == 0:
                pending.append(follower)
    if visited != len(by_id):
        errors.append(issue("dependency_cycle"))
    else:
        positions = {car_id: index for index, car_id in enumerate(order)}
        ancestors = {car_id: 0 for car_id in by_id}
        for car_id in order:
            for dependency in by_id[car_id]["depends_on"]:
                if dependency in by_id:
                    ancestors[car_id] |= ancestors[dependency] | (1 << positions[dependency])
        candidates = sorted(valid_cars, key=lambda car: car["id"])
        for index, left in enumerate(candidates):
            for right in candidates[index + 1:]:
                both_active = left["status"] in ACTIVE and right["status"] in ACTIVE
                both_runnable = left["status"] in ACTIVE | {"planned"} and right["status"] in ACTIVE | {"planned"}
                ordered = (
                    bool(ancestors[left["id"]] & (1 << positions[right["id"]]))
                    or bool(ancestors[right["id"]] & (1 << positions[left["id"]]))
                )
                if not both_active and (not both_runnable or ordered):
                    continue
                for left_scope in sorted(set(left["scope"])):
                    for right_scope in sorted(set(right["scope"])):
                        if scopes_overlap(left_scope, right_scope):
                            errors.append(issue(
                                "scope_ownership_overlap",
                                car_id=left["id"],
                                detail=f"{right['id']}:{left_scope}:{right_scope}",
                            ))
    wip = evaluate_writer_wip(plan, repo, by_id)
    errors.extend(wip.errors)
    if sum(car["status"] in ACTIVE for car in valid_cars) > wip.effective:
        errors.append(issue("writer_wip_exceeded"))
    return errors


def readiness(plan: dict, repo: Path | None = None) -> tuple[list[dict], list[dict]]:
    by_id = {car["id"]: car for car in plan["cars"]}
    ready, waiting = [], []
    active = sum(car["status"] in ACTIVE for car in plan["cars"])
    writer_wip = evaluate_writer_wip(plan, repo, by_id).effective
    for car in plan["cars"]:
        if car["status"] != "planned":
            continue
        blockers = [dep for dep in car["depends_on"] if by_id[dep]["status"] not in SATISFIED]
        if active >= writer_wip:
            blockers.append("writer_wip")
        item = {"id": car["id"], "waiting_on": blockers}
        (ready if not blockers else waiting).append(item)
    return ready, waiting


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "ready", "release-check"):
        command = sub.add_parser(name)
        command.add_argument("plan", type=Path)
        if name == "release-check":
            command.add_argument("car_id")
        command.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    plan, errors = load(args.plan)
    repo = repo_root(args.plan)
    if plan is not None:
        errors.extend(validate(plan, repo))
    result: dict = {"ok": not errors, "errors": errors}
    if not errors and args.command == "ready":
        result["ready"], result["waiting"] = readiness(plan, repo)
    if not errors and args.command == "release-check":
        matches = [car for car in plan["cars"] if car["id"] == args.car_id]
        if not matches:
            result = {"ok": False, "errors": [issue("unknown_car", car_id=args.car_id)]}
        elif matches[0]["status"] not in {"accepted", "releasing", "canary"}:
            result = {"ok": False, "errors": [issue("not_release_candidate", car_id=args.car_id)]}
    if args.json:
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    elif result["ok"]:
        print("OK")
    else:
        for error in result["errors"]:
            print(":".join(str(error[key]) for key in ("code", "car_id", "detail") if key in error), file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
