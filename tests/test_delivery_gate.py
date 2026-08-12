from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/delivery_gate.py"


def module():
    spec = importlib.util.spec_from_file_location("delivery_gate", SCRIPT)
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def plan() -> dict:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    car = {
        "id": "N0", "title": "isolation", "status": "planned", "depends_on": [],
        "scope": ["docs"], "acceptance": [{"command": "true", "passed": False}],
        "rollback": "git revert <fixed_sha>", "production_impact": "none",
        "owner_instance_id": None, "reviewer_instance_id": None, "base_sha": None,
        "fixed_sha": None, "cross_module_block_count": 0, "release_started_at": None,
        "user_acceptance_required": False, "user_acceptance_evidence": None,
    }
    return {
        "schema_version": 1, "goal_id": "cockpit-next", "user_journey": "safe upgrade",
        "non_goals": ["production deployment"],
        "baseline": {"main_sha": head, "production_version": "0.3.3", "production_source_sha": "1" * 40},
        "limits": {"writer_wip": 2, "release_minutes": 15, "cross_module_blocks_before_reslice": 2},
        "cars": [car],
    }


def codes(value: dict, *, now=None) -> set[str]:
    return {item["code"] for item in module().validate(value, ROOT, now=now)}


def test_valid_plan_and_ready_output(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan()))
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "ready", str(path), "--json"],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "errors": [], "ok": True, "ready": [{"id": "N0", "waiting_on": []}], "waiting": [],
    }


def test_unknown_root_field_is_rejected_before_semantic_validation() -> None:
    value = plan()
    value["surprise"] = True
    assert "unknown_field" in codes(value)


def test_duplicates_unknown_dependency_and_cycle_are_rejected() -> None:
    value = plan()
    duplicate = dict(value["cars"][0], depends_on=["N0"])
    value["cars"].append(duplicate)
    found = codes(value)
    assert {"duplicate_car_id", "dependency_cycle"} <= found

    value = plan()
    value["cars"][0]["depends_on"] = ["missing"]
    assert "unknown_dependency" in codes(value)


def test_missing_delivery_controls_are_rejected() -> None:
    value = plan()
    value["cars"][0].update(scope=[], acceptance=[], rollback="", production_impact="maybe")
    assert {"invalid_scope", "missing_acceptance", "missing_rollback", "invalid_production_impact"} <= codes(value)


def test_malformed_nested_types_return_codes_instead_of_crashing(tmp_path: Path) -> None:
    value = plan()
    value["cars"][0].update(depends_on=None, acceptance=["true"], cross_module_block_count="two")
    path = tmp_path / "malformed.json"
    path.write_text(json.dumps(value))
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "check", str(path), "--json"],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 1
    assert result.stderr == ""
    assert {"invalid_depends_on", "invalid_acceptance", "invalid_block_count"} <= {
        item["code"] for item in json.loads(result.stdout)["errors"]
    }


def test_active_owner_reviewer_sha_and_wip_gates() -> None:
    value = plan()
    car = value["cars"][0]
    car["status"] = "review"
    car["owner_instance_id"] = "opaque"
    value["cars"] = [dict(car, id=f"C{i}") for i in range(3)]
    found = codes(value)
    assert {"independent_reviewer_required", "exact_sha_required", "writer_wip_exceeded"} <= found


def test_dependency_acceptance_reslice_and_user_gates() -> None:
    value = plan()
    first = value["cars"][0]
    second = dict(first, id="R0", status="user_accepted", depends_on=["N0"],
                  owner_instance_id="owner", reviewer_instance_id="reviewer",
                  base_sha="a" * 40, fixed_sha="b" * 40,
                  cross_module_block_count=2, user_acceptance_required=True)
    value["cars"].append(second)
    found = codes(value)
    assert {"dependency_not_satisfied", "reslice_required", "user_acceptance_evidence_required"} <= found


def test_release_timeout_is_stable(monkeypatch) -> None:
    import datetime as dt
    value = plan()
    car = value["cars"][0]
    head = value["baseline"]["main_sha"]
    car.update(status="releasing", owner_instance_id="owner", reviewer_instance_id="reviewer",
               base_sha=head, fixed_sha=head, release_started_at="2026-08-12T00:00:00Z")
    car["acceptance"][0]["passed"] = True
    assert "release_timeout" in codes(value, now=dt.datetime(2026, 8, 12, 0, 16, tzinfo=dt.timezone.utc))
