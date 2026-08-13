#!/usr/bin/env python3
"""Read-only validator for versioned delivery plans."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
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


def effective_writer_wip(plan: dict, by_id: dict[str, dict] | None = None) -> int:
    limits = plan["limits"]
    effective = limits["writer_wip"]
    if plan["schema_version"] != 2:
        return effective
    cars = by_id if by_id is not None else {car["id"]: car for car in plan["cars"]}
    for gate in limits.get("writer_wip_gates", []):
        if not isinstance(gate, dict) or set(gate) != WRITER_WIP_GATE_FIELDS:
            break
        if (
            not isinstance(gate["car_id"], str)
            or type(gate["from"]) is not int
            or type(gate["to"]) is not int
            or effective != gate["from"]
        ):
            break
        car = cars.get(gate["car_id"])
        if not car or car["status"] not in SATISFIED:
            break
        effective = gate["to"]
    return effective


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
    if sum(car["status"] in ACTIVE for car in valid_cars) > effective_writer_wip(plan, by_id):
        errors.append(issue("writer_wip_exceeded"))
    return errors


def readiness(plan: dict) -> tuple[list[dict], list[dict]]:
    by_id = {car["id"]: car for car in plan["cars"]}
    ready, waiting = [], []
    active = sum(car["status"] in ACTIVE for car in plan["cars"])
    writer_wip = effective_writer_wip(plan, by_id)
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
    if plan is not None:
        errors.extend(validate(plan, repo_root(args.plan)))
    result: dict = {"ok": not errors, "errors": errors}
    if not errors and args.command == "ready":
        result["ready"], result["waiting"] = readiness(plan)
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
