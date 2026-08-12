#!/usr/bin/env python3
"""Read-only validator for versioned delivery plans."""

from __future__ import annotations

import argparse
import datetime as dt
import json
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
LIMIT_FIELDS = {"writer_wip", "release_minutes", "cross_module_blocks_before_reslice"}
ACCEPTANCE_FIELDS = {"command", "passed"}


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


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], text=True, capture_output=True, check=False,
    )


def parse_time(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else None
    except ValueError:
        return None


def load(path: Path) -> tuple[dict | None, list[dict]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, [issue("invalid_json", detail=str(exc))]
    if not isinstance(value, dict):
        return None, [issue("invalid_root", detail="expected object")]
    return value, []


def validate(plan: dict, repo: Path, now: dt.datetime | None = None) -> list[dict]:
    errors: list[dict] = []
    if not exact_fields(plan, ROOT_FIELDS, "invalid_root", errors):
        return errors
    if plan["schema_version"] != 1:
        errors.append(issue("unsupported_schema_version"))
    for field in ("goal_id", "user_journey"):
        if not isinstance(plan[field], str) or not plan[field].strip():
            errors.append(issue("invalid_field", detail=field))
    if not strings(plan["non_goals"]):
        errors.append(issue("invalid_field", detail="non_goals"))
    baseline = plan["baseline"]
    if exact_fields(baseline, BASELINE_FIELDS, "invalid_baseline", errors):
        if not is_sha(baseline["main_sha"]) or not is_sha(baseline["production_source_sha"]):
            errors.append(issue("invalid_baseline_sha"))
        if not isinstance(baseline["production_version"], str) or not baseline["production_version"]:
            errors.append(issue("invalid_field", detail="baseline.production_version"))
    limits = plan["limits"]
    if exact_fields(limits, LIMIT_FIELDS, "invalid_limits", errors):
        expected = {"writer_wip": 2, "release_minutes": 15, "cross_module_blocks_before_reslice": 2}
        for field, value in expected.items():
            if limits[field] != value:
                errors.append(issue("invalid_limit", detail=f"{field}={limits[field]!r}"))
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
        if not car_id or car_id.startswith("#"):
            errors.append(issue("invalid_car_id", car_id=car_id))
        elif car_id in ids:
            errors.append(issue("duplicate_car_id", car_id=car_id))
        ids.add(car_id)
        structurally_valid = True
        if car["status"] not in STATUSES:
            errors.append(issue("invalid_status", car_id=car_id))
        if not strings(car["depends_on"], allow_empty=True):
            errors.append(issue("invalid_depends_on", car_id=car_id))
            structurally_valid = False
        if not strings(car["scope"]) or any(Path(p).is_absolute() or ".." in Path(p).parts for p in car["scope"]):
            errors.append(issue("invalid_scope", car_id=car_id))
        acceptance = car["acceptance"]
        if not isinstance(acceptance, list) or not acceptance:
            errors.append(issue("missing_acceptance", car_id=car_id))
            structurally_valid = False
        else:
            for item in acceptance:
                if not exact_fields(item, ACCEPTANCE_FIELDS, "invalid_acceptance", errors, car_id=car_id):
                    structurally_valid = False
                    continue
                if not isinstance(item["command"], str) or not item["command"].strip() or not isinstance(item["passed"], bool):
                    errors.append(issue("invalid_acceptance", car_id=car_id))
                    structurally_valid = False
        if not isinstance(car["rollback"], str) or not car["rollback"].strip():
            errors.append(issue("missing_rollback", car_id=car_id))
        if car["production_impact"] not in {"none", "dark", "canary", "release"}:
            errors.append(issue("invalid_production_impact", car_id=car_id))
        for field in ("owner_instance_id", "reviewer_instance_id", "base_sha", "fixed_sha", "release_started_at", "user_acceptance_evidence"):
            if car[field] is not None and (not isinstance(car[field], str) or not car[field].strip()):
                errors.append(issue("invalid_field", car_id=car_id, detail=field))
        if not isinstance(car["cross_module_block_count"], int) or car["cross_module_block_count"] < 0:
            errors.append(issue("invalid_block_count", car_id=car_id))
            structurally_valid = False
        if not isinstance(car["user_acceptance_required"], bool):
            errors.append(issue("invalid_field", car_id=car_id, detail="user_acceptance_required"))
            structurally_valid = False
        if not isinstance(car["status"], str):
            structurally_valid = False
        if structurally_valid:
            valid_cars.append(car)

    by_id = {car["id"]: car for car in valid_cars if isinstance(car["id"], str)}
    for car in valid_cars:
        car_id, status = car["id"], car["status"]
        unknown = sorted(set(car["depends_on"]) - by_id.keys())
        if unknown:
            errors.append(issue("unknown_dependency", car_id=car_id, detail=",".join(unknown)))
        if status in ACTIVE | SHA_STATUSES and not car["owner_instance_id"]:
            errors.append(issue("owner_required", car_id=car_id))
        if status in SHA_STATUSES:
            if not car["reviewer_instance_id"] or car["reviewer_instance_id"] == car["owner_instance_id"]:
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
                        p == scope.rstrip("/") or p.startswith(scope.rstrip("/") + "/") for scope in car["scope"]
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
                if current.astimezone(dt.timezone.utc) - started.astimezone(dt.timezone.utc) > dt.timedelta(minutes=limits["release_minutes"]):
                    errors.append(issue("release_timeout", car_id=car_id))
        if car["cross_module_block_count"] >= limits["cross_module_blocks_before_reslice"] and status not in {"blocked", "cancelled"}:
            errors.append(issue("reslice_required", car_id=car_id))
        if status == "user_accepted":
            if not car["user_acceptance_required"] or not car["user_acceptance_evidence"]:
                errors.append(issue("user_acceptance_evidence_required", car_id=car_id))

    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(car_id: str) -> bool:
        if car_id in visiting:
            return True
        if car_id in visited:
            return False
        visiting.add(car_id)
        cycle = any(dep in by_id and visit(dep) for dep in by_id[car_id]["depends_on"])
        visiting.remove(car_id)
        visited.add(car_id)
        return cycle
    if any(visit(car_id) for car_id in by_id):
        errors.append(issue("dependency_cycle"))
    if sum(car["status"] in ACTIVE for car in valid_cars) > limits["writer_wip"]:
        errors.append(issue("writer_wip_exceeded"))
    return errors


def readiness(plan: dict) -> tuple[list[dict], list[dict]]:
    by_id = {car["id"]: car for car in plan["cars"]}
    ready, waiting = [], []
    active = sum(car["status"] in ACTIVE for car in plan["cars"])
    for car in plan["cars"]:
        if car["status"] != "planned":
            continue
        blockers = [dep for dep in car["depends_on"] if by_id[dep]["status"] not in SATISFIED]
        if active >= plan["limits"]["writer_wip"]:
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
        errors.extend(validate(plan, args.plan.resolve().parent.parent))
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
