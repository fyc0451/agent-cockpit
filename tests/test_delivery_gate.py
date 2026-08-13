from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


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


def gated_plan(*, gate_status: str = "review", extra_gates: int = 0) -> dict:
    value = plan()
    value["schema_version"] = 2
    value["limits"]["writer_wip_gates"] = [
        {"car_id": "DELIVERY-002-wip3-gate", "from": 2, "to": 3},
    ]
    head = value["baseline"]["main_sha"]
    gate = dict(
        value["cars"][0],
        id="DELIVERY-002-wip3-gate",
        status=gate_status,
        scope=["scripts/delivery_gate.py"],
        owner_instance_id="i-aaaaaaaaaaaaaaaaaaaaaaaaaa",
        reviewer_instance_id="i-bbbbbbbbbbbbbbbbbbbbbbbbbb",
        base_sha=head,
        fixed_sha=head,
        acceptance=[{"command": "true", "passed": gate_status == "accepted"}],
    )
    value["cars"] = [gate]
    if extra_gates:
        value["limits"]["writer_wip_gates"].append(
            {"car_id": "DELIVERY-003-wip4-gate", "from": 3, "to": 4},
        )
        value["cars"].append(dict(
            gate,
            id="DELIVERY-003-wip4-gate",
            status="planned",
            depends_on=["DELIVERY-002-wip3-gate"],
            scope=["docs/provider-ownership.md"],
            owner_instance_id=None,
            reviewer_instance_id=None,
            base_sha=None,
            fixed_sha=None,
            acceptance=[{"command": "true", "passed": False}],
        ))
    return value


PARTITIONS = (
    ("operation", "OPERATION-001-journal", "operation"),
    ("runtime_provider", "RUNTIME-002-provider", "runtime_provider"),
    ("event", "EVENT-001-journal", "event"),
    ("memory", "MEMORY-001-store", "memory"),
)
GLOBAL_HOTSPOTS = [
    "agent_cockpit/runtime_paths.py",
    "agent_cockpit/server.py",
    "agent_cockpit/store_schema.py",
    "server.py",
]


def provider_sidecar() -> dict:
    return {
        "schema_version": 1,
        "gate_id": "DELIVERY-003-wip4-gate",
        "transition": {"from": 3, "to": 4},
        "global_hotspots": GLOBAL_HOTSPOTS,
        "partitions": [
            {
                "id": partition_id,
                "car_id": car_id,
                "scopes": sorted([
                    f"agent_cockpit/{stem}_api.py",
                    f"agent_cockpit/{stem}_store.py",
                    f"docs/contracts/{stem.replace('_', '-')}-v1.md",
                    f"tests/test_{stem}.py",
                ]),
                "store_migration_scope": f"agent_cockpit/{stem}_store.py",
                "entrypoint_scope": f"agent_cockpit/{stem}_api.py",
            }
            for partition_id, car_id, stem in PARTITIONS
        ],
    }


def _run_git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _commit(repo: Path, message: str) -> str:
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=repo, check=True,
    )
    return _run_git(repo, "rev-parse", "HEAD")


def _future_car(template: dict, partition: dict) -> dict:
    return dict(
        template,
        id=partition["car_id"],
        title=f"Plan {partition['id']} ownership without implementing it",
        status="planned",
        depends_on=["DELIVERY-003-wip4-gate"],
        scope=list(partition["scopes"]),
        owner_instance_id=None,
        reviewer_instance_id=None,
        base_sha=None,
        fixed_sha=None,
        acceptance=[{"command": "true", "passed": False}],
    )


def _candidate_plan(base_sha: str, evidence: dict) -> dict:
    value = gated_plan(gate_status="accepted", extra_gates=1)
    value["baseline"]["main_sha"] = base_sha
    first, second = value["cars"]
    first.update(base_sha=base_sha, fixed_sha=base_sha)
    second.update(
        status="in_progress",
        depends_on=["DELIVERY-002-wip3-gate"],
        scope=[
            ".delivery/cockpit-product-v3.json",
            ".delivery/provider-ownership-v1.json",
        ],
        owner_instance_id="i-cccccccccccccccccccccccccc",
        reviewer_instance_id=None,
        base_sha=base_sha,
        fixed_sha=None,
        acceptance=[{"command": "true", "passed": False}],
    )
    value["cars"].extend(
        _future_car(second, partition) for partition in evidence["partitions"]
    )
    return value


def wip4_repo(
    tmp_path: Path, *, status: str = "review", evidence: object = None,
    base_evidence: object = None, mutate_candidate=None,
) -> tuple[Path, dict, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Delivery Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "delivery@example.invalid"], cwd=repo, check=True)
    delivery = repo / ".delivery"
    delivery.mkdir()
    (repo / "seed").write_text("base\n", encoding="utf-8")
    if base_evidence is not None:
        (delivery / "provider-ownership-v1.json").write_text(
            json.dumps(base_evidence), encoding="utf-8",
        )
    base_sha = _commit(repo, "base")

    sidecar = provider_sidecar() if evidence is None else evidence
    candidate = _candidate_plan(base_sha, provider_sidecar())
    if mutate_candidate is not None:
        mutate_candidate(candidate)
    (delivery / "cockpit-product-v3.json").write_text(
        json.dumps(candidate), encoding="utf-8",
    )
    if sidecar is not False:
        (delivery / "provider-ownership-v1.json").write_text(
            json.dumps(sidecar), encoding="utf-8",
        )
    fixed_sha = _commit(repo, "fixed")

    current = copy.deepcopy(candidate)
    gate = next(car for car in current["cars"] if car["id"] == "DELIVERY-003-wip4-gate")
    gate.update(
        status=status,
        reviewer_instance_id="i-dddddddddddddddddddddddddd",
        fixed_sha=fixed_sha,
        acceptance=[{"command": "true", "passed": status in {"accepted", "user_accepted"}}],
    )
    (delivery / "cockpit-product-v3.json").write_text(
        json.dumps(current), encoding="utf-8",
    )
    return repo, current, base_sha, fixed_sha


def _wip4_gate(value: dict) -> dict:
    return next(car for car in value["cars"] if car["id"] == "DELIVERY-003-wip4-gate")


def _evaluation(value: dict, repo: Path):
    gate = module()
    by_id = {car["id"]: car for car in value["cars"]}
    return gate.evaluate_writer_wip(value, repo, by_id)


def test_wip4_valid_fixed_tree_evidence_enables_four_only_after_acceptance(
    tmp_path: Path,
) -> None:
    repo, value, _base, _fixed = wip4_repo(tmp_path, status="review")
    gate = module()
    evaluation = _evaluation(value, repo)
    assert evaluation.effective == 3
    assert evaluation.errors == ()

    _wip4_gate(value)["status"] = "accepted"
    _wip4_gate(value)["acceptance"][0]["passed"] = True
    evaluation = _evaluation(value, repo)
    assert evaluation.effective == 4
    assert evaluation.errors == ()

    template = value["cars"][-1]
    value["cars"].extend([
        dict(
            template,
            id=f"active-{index}",
            status="in_progress",
            depends_on=[],
            scope=[f"active/{index}"],
            owner_instance_id=f"i-{'efg'[index] * 26}",
        )
        for index in range(3)
    ])
    ready, waiting = gate.readiness(value, repo)
    assert any(item["id"] == "OPERATION-001-journal" for item in ready)
    assert not any(
        item["id"] == "OPERATION-001-journal" and "writer_wip" in item["waiting_on"]
        for item in waiting
    )


def test_wip4_preserves_v1_and_wip3_capacity(tmp_path: Path) -> None:
    gate = module()
    assert gate.effective_writer_wip(plan(), ROOT) == 2
    wip3 = gated_plan(gate_status="accepted")
    assert gate.effective_writer_wip(wip3, ROOT) == 3
    repo, value, _base, _fixed = wip4_repo(tmp_path, status="review")
    assert gate.effective_writer_wip(value, repo) == 3


def test_wip4_transition_cannot_bypass_evidence_with_another_delivery_id() -> None:
    value = gated_plan(gate_status="accepted", extra_gates=1)
    value["limits"]["writer_wip_gates"][1]["car_id"] = "DELIVERY-999-wip4-gate"
    value["cars"][1]["id"] = "DELIVERY-999-wip4-gate"
    value["cars"][1].update(
        status="accepted",
        owner_instance_id="i-cccccccccccccccccccccccccc",
        reviewer_instance_id="i-dddddddddddddddddddddddddd",
        base_sha=value["baseline"]["main_sha"],
        fixed_sha=value["baseline"]["main_sha"],
        acceptance=[{"command": "true", "passed": True}],
    )
    gate = module()
    assert "writer_wip_gate_required" in {
        item["code"] for item in gate.validate(value, ROOT)
    }
    assert gate.effective_writer_wip(value, ROOT) == 3


@pytest.mark.parametrize(
    "transition",
    (
        {"car_id": "DELIVERY-003-wip4-gate", "from": 2, "to": 4},
        {"car_id": "DELIVERY-003-wip4-gate", "from": 3, "to": 5},
        {"car_id": "DELIVERY-003-wip4-gate", "from": True, "to": 4},
    ),
)
def test_shared_evaluator_rejects_malformed_wip4_transition_directly(
    tmp_path: Path, transition: dict,
) -> None:
    repo, value, _base, _fixed = wip4_repo(tmp_path, status="accepted")
    value["limits"]["writer_wip_gates"][1] = transition
    gate = module()
    assert gate.effective_writer_wip(value, repo) == 3
    template = value["cars"][-1]
    value["cars"].extend([
        dict(template, id=f"active-{index}", status="in_progress", depends_on=[],
             scope=[f"active/{index}"], owner_instance_id=f"i-{'efg'[index] * 26}")
        for index in range(3)
    ])
    _ready, waiting = gate.readiness(value, repo)
    assert any(
        item["id"] == "OPERATION-001-journal" and "writer_wip" in item["waiting_on"]
        for item in waiting
    )


def test_wip4_rejects_noop_base_fixed_sha(tmp_path: Path) -> None:
    repo, value, base, _fixed = wip4_repo(tmp_path, status="accepted")
    _wip4_gate(value)["fixed_sha"] = base
    evaluation = _evaluation(value, repo)
    assert evaluation.effective == 3
    assert {item["code"] for item in evaluation.errors} == {
        "provider_ownership_evidence_required"
    }


def test_wip4_rejects_uncommitted_and_unchanged_sidecar(tmp_path: Path) -> None:
    repo, value, _base, _fixed = wip4_repo(
        tmp_path / "uncommitted", status="accepted", evidence=False,
    )
    (repo / ".delivery/provider-ownership-v1.json").write_text(
        json.dumps(provider_sidecar()), encoding="utf-8",
    )
    assert _evaluation(value, repo).effective == 3
    assert {item["code"] for item in _evaluation(value, repo).errors} == {
        "provider_ownership_evidence_required"
    }

    evidence = provider_sidecar()
    repo, value, _base, _fixed = wip4_repo(
        tmp_path / "unchanged", status="accepted",
        evidence=evidence, base_evidence=evidence,
    )
    assert _evaluation(value, repo).effective == 3
    assert {item["code"] for item in _evaluation(value, repo).errors} == {
        "provider_ownership_evidence_required"
    }


@pytest.mark.parametrize("base_sha", ["f" * 40, "non-descendant"])
def test_wip4_rejects_missing_or_non_descendant_base(
    tmp_path: Path, base_sha: str,
) -> None:
    repo, value, _base, _fixed = wip4_repo(tmp_path, status="accepted")
    if base_sha == "non-descendant":
        subprocess.run(["git", "checkout", "--orphan", "other"], cwd=repo, check=True,
                       stdout=subprocess.DEVNULL)
        (repo / "other").write_text("other\n", encoding="utf-8")
        base_sha = _commit(repo, "other")
    _wip4_gate(value)["base_sha"] = base_sha
    evaluation = _evaluation(value, repo)
    assert evaluation.effective == 3
    assert "provider_ownership_evidence_required" in {
        item["code"] for item in evaluation.errors
    }


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: [],
        lambda value: {**value, "extra": True},
        lambda value: {key: item for key, item in value.items() if key != "gate_id"},
        lambda value: {**value, "schema_version": True},
        lambda value: {**value, "gate_id": "OTHER"},
        lambda value: {**value, "transition": {"from": 3, "to": 5}},
        lambda value: {**value, "partitions": value["partitions"][:3]},
        lambda value: {**value, "partitions": list(reversed(value["partitions"]))},
        lambda value: {**value, "global_hotspots": value["global_hotspots"][:-1]},
    ),
)
def test_wip4_provider_ownership_schema_is_strict(
    tmp_path: Path, mutation,
) -> None:
    evidence = mutation(provider_sidecar())
    repo, value, _base, _fixed = wip4_repo(
        tmp_path, status="accepted", evidence=evidence,
    )
    evaluation = _evaluation(value, repo)
    assert evaluation.effective == 3
    assert "invalid_provider_ownership_evidence" in {
        item["code"] for item in evaluation.errors
    }


def test_wip4_duplicate_sidecar_key_is_invalid(tmp_path: Path) -> None:
    repo, value, _base, _fixed = wip4_repo(tmp_path, status="accepted")
    sidecar = repo / ".delivery/provider-ownership-v1.json"
    sidecar.write_text(
        '{"schema_version":1,"schema_version":1}', encoding="utf-8",
    )
    fixed = _commit(repo, "duplicate sidecar key")
    _wip4_gate(value)["fixed_sha"] = fixed
    assert "invalid_provider_ownership_evidence" in {
        item["code"] for item in _evaluation(value, repo).errors
    }


@pytest.mark.parametrize("overlap", ["partition", "migration", "entrypoint", "hotspot"])
def test_wip4_rejects_partition_migration_entrypoint_and_global_hotspot_overlap(
    tmp_path: Path, overlap: str,
) -> None:
    evidence = provider_sidecar()
    if overlap == "partition":
        evidence["partitions"][1]["scopes"][0] = evidence["partitions"][0]["scopes"][0]
        evidence["partitions"][1]["scopes"].sort()
    elif overlap == "migration":
        evidence["partitions"][1]["store_migration_scope"] = evidence["partitions"][0]["store_migration_scope"]
    elif overlap == "entrypoint":
        evidence["partitions"][1]["entrypoint_scope"] = evidence["partitions"][0]["entrypoint_scope"]
    else:
        evidence["partitions"][0]["scopes"][0] = GLOBAL_HOTSPOTS[1]
        evidence["partitions"][0]["scopes"].sort()
    repo, value, _base, _fixed = wip4_repo(
        tmp_path, status="accepted", evidence=evidence,
    )
    evaluation = _evaluation(value, repo)
    assert evaluation.effective == 3
    assert "provider_ownership_overlap" in {
        item["code"] for item in evaluation.errors
    }


@pytest.mark.parametrize("mismatch", ["missing", "dependency", "status", "scope", "drift"])
def test_wip4_rejects_future_car_binding_and_current_plan_drift(
    tmp_path: Path, mismatch: str,
) -> None:
    def fixed_status(candidate: dict) -> None:
        target = next(car for car in candidate["cars"] if car["id"] == "MEMORY-001-store")
        target["status"] = "accepted"

    repo, value, _base, _fixed = wip4_repo(
        tmp_path, status="accepted",
        mutate_candidate=fixed_status if mismatch == "status" else None,
    )
    target = next(car for car in value["cars"] if car["id"] == "MEMORY-001-store")
    if mismatch == "missing":
        value["cars"].remove(target)
    elif mismatch == "dependency":
        target["depends_on"] = []
    elif mismatch != "status":
        target["scope"] = [*target["scope"], "memory-extra"]
    evaluation = _evaluation(value, repo)
    assert evaluation.effective == 3
    assert "provider_ownership_car_mismatch" in {
        item["code"] for item in evaluation.errors
    }


@pytest.mark.parametrize("where", ["fixed", "current"])
def test_wip4_rejects_extra_or_drifted_future_car_dependencies(
    tmp_path: Path, where: str,
) -> None:
    def fixed_extra(candidate: dict) -> None:
        candidate["cars"].append(dict(
            candidate["cars"][-1], id="PREREQ", status="accepted",
            depends_on=[], scope=["prereq"],
            owner_instance_id="i-eeeeeeeeeeeeeeeeeeeeeeeeee",
            reviewer_instance_id="i-ffffffffffffffffffffffffff",
            base_sha=candidate["baseline"]["main_sha"],
            fixed_sha=candidate["baseline"]["main_sha"],
            acceptance=[{"command": "true", "passed": True}],
        ))
        target = next(car for car in candidate["cars"] if car["id"] == "MEMORY-001-store")
        target["depends_on"] = ["DELIVERY-003-wip4-gate", "PREREQ"]

    repo, value, _base, _fixed = wip4_repo(
        tmp_path, status="accepted", mutate_candidate=fixed_extra if where == "fixed" else None,
    )
    if where == "current":
        value["cars"].append(dict(
            value["cars"][-1], id="PREREQ", status="accepted",
            depends_on=[], scope=["prereq"],
            owner_instance_id="i-eeeeeeeeeeeeeeeeeeeeeeeeee",
            reviewer_instance_id="i-ffffffffffffffffffffffffff",
            base_sha=value["baseline"]["main_sha"], fixed_sha=value["baseline"]["main_sha"],
            acceptance=[{"command": "true", "passed": True}],
        ))
        target = next(car for car in value["cars"] if car["id"] == "MEMORY-001-store")
        target["depends_on"] = ["DELIVERY-003-wip4-gate", "PREREQ"]
    assert "provider_ownership_car_mismatch" in {
        item["code"] for item in _evaluation(value, repo).errors
    }


def test_wip4_rejects_duplicate_fixed_tree_car_ids(tmp_path: Path) -> None:
    def duplicate(candidate: dict) -> None:
        target = next(car for car in candidate["cars"] if car["id"] == "MEMORY-001-store")
        candidate["cars"].insert(0, dict(target, scope=["agent_cockpit/server.py"]))

    repo, value, _base, _fixed = wip4_repo(
        tmp_path, status="accepted", mutate_candidate=duplicate,
    )
    assert "invalid_provider_ownership_evidence" in {
        item["code"] for item in _evaluation(value, repo).errors
    }


@pytest.mark.parametrize(
    ("field", "bad"),
    (("status", []), ("depends_on", None), ("scope", None), ("scope", "agent_cockpit/operation_api.py")),
)
def test_wip4_rejects_malformed_fixed_tree_runnable_car_shape(
    tmp_path: Path, field: str, bad: object,
) -> None:
    def malformed(candidate: dict) -> None:
        template = candidate["cars"][-1]
        candidate["cars"].append(dict(
            template,
            id="OTHER-001",
            status="in_progress",
            depends_on=[],
            scope=[provider_sidecar()["partitions"][0]["scopes"][0]],
            owner_instance_id="i-eeeeeeeeeeeeeeeeeeeeeeeeee",
        ))
        candidate["cars"][-1][field] = bad

    repo, value, _base, _fixed = wip4_repo(
        tmp_path, status="accepted", mutate_candidate=malformed,
    )
    evaluation = _evaluation(value, repo)
    assert evaluation.effective == 3
    assert "invalid_provider_ownership_evidence" in {
        item["code"] for item in evaluation.errors
    }


@pytest.mark.parametrize("status", ["in_progress", "review", "accepted"])
def test_wip4_current_provider_status_may_advance_without_scope_drift(
    tmp_path: Path, status: str,
) -> None:
    repo, value, _base, _fixed = wip4_repo(tmp_path, status="accepted")
    target = next(car for car in value["cars"] if car["id"] == "MEMORY-001-store")
    target["status"] = status
    assert _evaluation(value, repo).effective == 4


def test_wip4_ignores_historical_accepted_wide_scope_but_rejects_runnable_conflict(
    tmp_path: Path,
) -> None:
    def accepted_wide(candidate: dict) -> None:
        template = candidate["cars"][-1]
        candidate["cars"].append(dict(
            template,
            id="HISTORY-001",
            status="accepted",
            depends_on=[],
            scope=["agent_cockpit"],
            owner_instance_id="i-eeeeeeeeeeeeeeeeeeeeeeeeee",
            reviewer_instance_id="i-ffffffffffffffffffffffffff",
            base_sha=candidate["baseline"]["main_sha"],
            fixed_sha=candidate["baseline"]["main_sha"],
            acceptance=[{"command": "true", "passed": True}],
        ))

    repo, value, _base, _fixed = wip4_repo(
        tmp_path / "history", status="accepted", mutate_candidate=accepted_wide,
    )
    assert _evaluation(value, repo).effective == 4

    def planned_conflict(candidate: dict) -> None:
        template = candidate["cars"][-1]
        candidate["cars"].append(dict(
            template,
            id="OTHER-001",
            status="planned",
            depends_on=[],
            scope=[provider_sidecar()["partitions"][0]["scopes"][0]],
            owner_instance_id=None,
            reviewer_instance_id=None,
            base_sha=None,
            fixed_sha=None,
            acceptance=[{"command": "true", "passed": False}],
        ))

    repo, value, _base, _fixed = wip4_repo(
        tmp_path / "runnable", status="accepted", mutate_candidate=planned_conflict,
    )
    assert _evaluation(value, repo).effective == 3
    assert "provider_ownership_overlap" in {
        item["code"] for item in _evaluation(value, repo).errors
    }


def test_wip4_ignores_planned_scope_conflict_with_unsatisfied_dependency(
    tmp_path: Path,
) -> None:
    def blocked_plan(candidate: dict) -> None:
        template = candidate["cars"][-1]
        candidate["cars"].append(dict(
            template, id="BLOCKER", status="planned", depends_on=[], scope=["blocker"],
            owner_instance_id=None, reviewer_instance_id=None, base_sha=None, fixed_sha=None,
        ))
        candidate["cars"].append(dict(
            template, id="FUTURE", status="planned", depends_on=["BLOCKER"],
            scope=[provider_sidecar()["partitions"][0]["scopes"][0]],
            owner_instance_id=None, reviewer_instance_id=None, base_sha=None, fixed_sha=None,
        ))

    repo, value, _base, _fixed = wip4_repo(
        tmp_path, status="accepted", mutate_candidate=blocked_plan,
    )
    assert _evaluation(value, repo).effective == 4


def test_validate_effective_and_readiness_share_wip4_evaluation(tmp_path: Path) -> None:
    repo, value, base, _fixed = wip4_repo(tmp_path, status="accepted")
    _wip4_gate(value)["fixed_sha"] = base
    gate = module()
    assert "provider_ownership_evidence_required" in {
        item["code"] for item in gate.validate(value, repo)
    }
    assert gate.effective_writer_wip(value, repo) == 3
    template = value["cars"][-1]
    value["cars"].extend([
        dict(template, id=f"active-{index}", status="in_progress", depends_on=[],
             scope=[f"active/{index}"], owner_instance_id=f"i-{'efg'[index] * 26}")
        for index in range(3)
    ])
    ready, waiting = gate.readiness(value, repo)
    assert not any(item["id"] == "OPERATION-001-journal" for item in ready)
    assert any(
        item["id"] == "OPERATION-001-journal" and "writer_wip" in item["waiting_on"]
        for item in waiting
    )


def test_wip4_evidence_error_is_stable_for_all_cli_commands(tmp_path: Path) -> None:
    repo, value, base, _fixed = wip4_repo(tmp_path, status="accepted")
    _wip4_gate(value)["fixed_sha"] = base
    path = repo / ".delivery/cockpit-product-v3.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    for command in ("check", "ready"):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), command, str(path), "--json"],
            cwd=repo, text=True, capture_output=True,
        )
        assert result.returncode == 1
        assert result.stderr == ""
        assert "provider_ownership_evidence_required" in {
            item["code"] for item in json.loads(result.stdout)["errors"]
        }
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "release-check", str(path),
         "DELIVERY-003-wip4-gate", "--json"],
        cwd=repo, text=True, capture_output=True,
    )
    assert result.returncode == 1
    assert result.stderr == ""
    assert "provider_ownership_evidence_required" in {
        item["code"] for item in json.loads(result.stdout)["errors"]
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

    gated = subprocess.run(
        [sys.executable, str(SCRIPT), "check", str(ROOT / "tests/fixtures/delivery_gate/valid_wip3_gated.json"), "--json"],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert gated.returncode == 0
    assert json.loads(gated.stdout) == {"errors": [], "ok": True}


def test_new_negative_fixtures_have_stable_codes() -> None:
    cases = {
        "invalid_21_overlapping_scope_ownership.json": "scope_ownership_overlap",
        "invalid_22_ungated_wip3.json": "writer_wip_gate_required",
    }
    fixture_root = ROOT / "tests/fixtures/delivery_gate"
    for name, code in cases.items():
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "check", str(fixture_root / name), "--json"],
            cwd=ROOT, text=True, capture_output=True,
        )
        assert result.returncode == 1
        assert code in {item["code"] for item in json.loads(result.stdout)["errors"]}
        assert result.stderr == ""


def test_release_check_json_is_stable(tmp_path: Path) -> None:
    value = plan()
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(value))
    command = [sys.executable, str(SCRIPT), "release-check", str(path)]
    planned = subprocess.run(command + ["N0", "--json"], cwd=ROOT, text=True, capture_output=True)
    assert planned.returncode == 1
    assert json.loads(planned.stdout)["errors"][0]["code"] == "not_release_candidate"
    unknown = subprocess.run(command + ["missing", "--json"], cwd=ROOT, text=True, capture_output=True)
    assert json.loads(unknown.stdout)["errors"][0]["code"] == "unknown_car"

    car = value["cars"][0]
    head = value["baseline"]["main_sha"]
    car.update(status="accepted", owner_instance_id="i-aaaaaaaaaaaaaaaaaaaaaaaaaa",
               reviewer_instance_id="i-bbbbbbbbbbbbbbbbbbbbbbbbbb", base_sha=head,
               fixed_sha=head)
    car["acceptance"][0]["passed"] = True
    path.write_text(json.dumps(value))
    accepted = subprocess.run(command + ["N0", "--json"], cwd=ROOT, text=True, capture_output=True)
    assert accepted.returncode == 0
    assert json.loads(accepted.stdout) == {"errors": [], "ok": True}


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


def test_large_dag_is_order_independent() -> None:
    value = plan()
    template = value["cars"][0]
    value["cars"] = [
        dict(template, id=f"C{i}", depends_on=[] if i == 0 else [f"C{i - 1}"])
        for i in range(1200)
    ]
    assert codes(value) == set()
    value["cars"].reverse()
    assert codes(value) == set()


def test_missing_delivery_controls_are_rejected() -> None:
    value = plan()
    value["cars"][0].update(scope=[], acceptance=[], rollback="", production_impact="maybe")
    assert {"invalid_scope", "missing_acceptance", "missing_rollback", "invalid_production_impact"} <= codes(value)

    for scope in (["."], ["docs", "docs/"], ["docs", "docs"]):
        value = plan()
        value["cars"][0]["scope"] = scope
        assert "invalid_scope" in codes(value)


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


def test_wrong_enum_and_limit_types_return_stable_codes() -> None:
    value = plan()
    value["cars"][0].update(status=[], production_impact={})
    assert {"invalid_status", "invalid_production_impact"} <= codes(value)

    value = plan()
    value["limits"]["writer_wip"] = "2"
    assert "invalid_limit" in codes(value)


def test_all_nested_wrong_types_return_json_errors(tmp_path: Path) -> None:
    cases = {
        "baseline": None, "limits": None, "cars": [None],
        "cars.0.id": [], "cars.0.title": None, "cars.0.status": [],
        "cars.0.depends_on": None, "cars.0.scope": None,
        "cars.0.acceptance": ["true"], "cars.0.rollback": [],
        "cars.0.production_impact": {}, "cars.0.owner_instance_id": [],
        "cars.0.reviewer_instance_id": {}, "cars.0.base_sha": [],
        "cars.0.fixed_sha": {}, "cars.0.cross_module_block_count": True,
        "cars.0.release_started_at": [], "cars.0.user_acceptance_required": 1,
        "cars.0.user_acceptance_evidence": [],
    }
    for label, bad in cases.items():
        value = plan()
        target, field = (value, label) if "." not in label else (value["cars"][0], label.rsplit(".", 1)[1])
        target[field] = bad
        path = tmp_path / f"{label.replace('.', '-')}.json"
        path.write_text(json.dumps(value))
        result = subprocess.run([sys.executable, str(SCRIPT), "check", str(path), "--json"], text=True, capture_output=True)
        assert result.returncode == 1, label
        assert json.loads(result.stdout)["errors"], label
        assert result.stderr == "", label


def test_duplicate_json_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version":1,"schema_version":1}')
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "check", str(path), "--json"],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["errors"][0]["code"] == "duplicate_json_key"

    path.write_text('{"schema_version":NaN}')
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "check", str(path), "--json"],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["errors"][0]["code"] == "invalid_json"


def test_active_owner_reviewer_sha_and_wip_gates() -> None:
    value = plan()
    car = value["cars"][0]
    car["status"] = "review"
    car["owner_instance_id"] = "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    value["cars"] = [dict(car, id=f"C{i}") for i in range(3)]
    found = codes(value)
    assert {"independent_reviewer_required", "exact_sha_required", "writer_wip_exceeded"} <= found


def test_wip_capacity_is_gated_and_extensible() -> None:
    value = gated_plan()
    assert module().effective_writer_wip(value) == 2

    gate = value["cars"][0]
    template = dict(
        gate,
        status="in_progress",
        reviewer_instance_id=None,
        base_sha=None,
        fixed_sha=None,
        acceptance=[{"command": "true", "passed": False}],
    )
    value["cars"].extend([
        dict(template, id=f"writer-{name}", depends_on=[gate["id"]], scope=[f"agent_cockpit/{name}.py"])
        for name in ("a", "b", "c")
    ])
    assert "writer_wip_exceeded" in codes(value)

    gate["status"] = "accepted"
    gate["acceptance"][0]["passed"] = True
    assert module().effective_writer_wip(value) == 3
    assert "writer_wip_exceeded" not in codes(value)
    ready, waiting = module().readiness(value)
    assert ready == waiting == []

    value = gated_plan(gate_status="accepted", extra_gates=1)
    assert module().effective_writer_wip(value) == 3
    second = value["cars"][1]
    second.update(
        status="accepted",
        owner_instance_id="i-cccccccccccccccccccccccccc",
        reviewer_instance_id="i-dddddddddddddddddddddddddd",
        base_sha=value["baseline"]["main_sha"],
        fixed_sha=value["baseline"]["main_sha"],
        acceptance=[{"command": "true", "passed": True}],
    )
    assert module().effective_writer_wip(value, ROOT) == 3


def test_readiness_uses_effective_capacity_before_and_after_acceptance() -> None:
    value = gated_plan()
    gate = value["cars"][0]
    active = dict(
        gate,
        id="active",
        status="in_progress",
        depends_on=[],
        scope=["agent_cockpit/active.py"],
        owner_instance_id="i-cccccccccccccccccccccccccc",
        reviewer_instance_id=None,
        base_sha=None,
        fixed_sha=None,
        acceptance=[{"command": "true", "passed": False}],
    )
    planned = dict(
        active,
        id="planned",
        status="planned",
        depends_on=[],
        scope=["agent_cockpit/planned.py"],
        owner_instance_id=None,
    )
    value["cars"].extend([active, planned])
    _, waiting = module().readiness(value)
    assert waiting == [{"id": "planned", "waiting_on": ["writer_wip"]}]

    gate["status"] = "accepted"
    gate["acceptance"][0]["passed"] = True
    ready, waiting = module().readiness(value)
    assert ready == [{"id": "planned", "waiting_on": []}]
    assert waiting == []


def test_ungated_or_malformed_capacity_is_rejected() -> None:
    value = gated_plan()
    value["limits"].pop("writer_wip_gates")
    assert "writer_wip_gate_required" in codes(value)

    for bad in (
        [],
        [{"car_id": "DELIVERY-002-wip3-gate", "from": 2, "to": 4}],
        [{"car_id": "OTHER", "from": 2, "to": 3}],
        [{"car_id": "DELIVERY-002-wip3-gate", "from": True, "to": 3}],
        [{"car_id": "DELIVERY-002-wip3-gate", "from": 2, "to": 3, "extra": 4}],
    ):
        value = gated_plan()
        value["limits"]["writer_wip_gates"] = bad
        assert "writer_wip_gate_required" in codes(value)


def test_oversized_gate_id_fails_closed_for_every_cli_command(tmp_path: Path) -> None:
    value = gated_plan()
    value["limits"]["writer_wip_gates"][0]["car_id"] = (
        "DELIVERY-002-wip" + "9" * 5000 + "-gate"
    )
    path = tmp_path / "oversized-gate.json"
    path.write_text(json.dumps(value))
    for command in ("check", "ready"):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), command, str(path), "--json"],
            cwd=ROOT, text=True, capture_output=True,
        )
        assert result.returncode == 1
        assert result.stderr == ""
        assert "writer_wip_gate_required" in {
            item["code"] for item in json.loads(result.stdout)["errors"]
        }

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "release-check", str(path), "missing", "--json"],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 1
    assert result.stderr == ""
    assert "writer_wip_gate_required" in {
        item["code"] for item in json.loads(result.stdout)["errors"]
    }


def test_scope_ownership_rejects_parallel_prefix_overlap() -> None:
    value = plan()
    template = value["cars"][0]
    value["cars"] = [
        dict(template, id="discovery", scope=["agent_cockpit/project"]),
        dict(template, id="legacy", scope=["agent_cockpit/project/import.py"]),
    ]
    errors = module().validate(value, ROOT)
    assert {
        (item.get("car_id"), item.get("detail"))
        for item in errors if item["code"] == "scope_ownership_overlap"
    } == {("discovery", "legacy:agent_cockpit/project:agent_cockpit/project/import.py")}


def test_scope_ownership_allows_serial_reuse_but_not_two_active_writers() -> None:
    value = plan()
    template = value["cars"][0]
    value["cars"] = [
        dict(template, id="first", scope=["docs/contracts"]),
        dict(template, id="second", depends_on=["first"], scope=["docs/contracts/file.md"]),
    ]
    assert "scope_ownership_overlap" not in codes(value)

    for car, owner in zip(value["cars"], ("i-aaaaaaaaaaaaaaaaaaaaaaaaaa", "i-bbbbbbbbbbbbbbbbbbbbbbbbbb")):
        car.update(status="in_progress", owner_instance_id=owner)
    assert "scope_ownership_overlap" in codes(value)


def test_scope_comparison_uses_path_parts_not_string_prefixes() -> None:
    value = plan()
    template = value["cars"][0]
    value["cars"] = [
        dict(template, id="api", scope=["agent_cockpit/project_registry_api.py"]),
        dict(template, id="store", scope=["agent_cockpit/project_registry_store.py"]),
    ]
    assert "scope_ownership_overlap" not in codes(value)

    value["cars"][1]["scope"] = ["agent_cockpit/project_registry_api.py/generated"]
    assert "scope_ownership_overlap" in codes(value)


def test_dependency_acceptance_reslice_and_user_gates() -> None:
    value = plan()
    first = value["cars"][0]
    second = dict(first, id="R0", status="user_accepted", depends_on=["N0"],
                  owner_instance_id="i-aaaaaaaaaaaaaaaaaaaaaaaaaa",
                  reviewer_instance_id="i-bbbbbbbbbbbbbbbbbbbbbbbbbb",
                  base_sha="a" * 40, fixed_sha="b" * 40,
                  cross_module_block_count=2, user_acceptance_required=True)
    value["cars"].append(second)
    found = codes(value)
    assert {"dependency_not_satisfied", "reslice_required", "user_acceptance_evidence_required"} <= found

    second["user_acceptance_evidence"] = "agent-authored claim"
    assert "user_acceptance_evidence_required" in codes(value)


def test_release_timeout_is_stable(monkeypatch) -> None:
    import datetime as dt
    value = plan()
    car = value["cars"][0]
    head = value["baseline"]["main_sha"]
    car.update(status="releasing", owner_instance_id="i-aaaaaaaaaaaaaaaaaaaaaaaaaa",
               reviewer_instance_id="i-bbbbbbbbbbbbbbbbbbbbbbbbbb",
               base_sha=head, fixed_sha=head, release_started_at="2026-08-12T00:00:00Z")
    car["acceptance"][0]["passed"] = True
    assert "release_timeout" in codes(value, now=dt.datetime(2026, 8, 12, 0, 16, tzinfo=dt.timezone.utc))
    car["release_started_at"] = "2099-01-01T00:00:00Z"
    assert "release_start_in_future" in codes(value, now=dt.datetime(2026, 8, 12, tzinfo=dt.timezone.utc))
