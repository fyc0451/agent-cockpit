import math
import os

import pytest

import coordination


def _create(tmp_path, **overrides):
    values = {
        "project_key": str(tmp_path),
        "assignment": "修复可靠发送失败后的补偿路径",
        "assignee": "codex-worker",
        "expected_reply": "提交 SHA、测试结果和剩余风险",
        "deadline": 2_000.0,
        "now": 1_000.0,
    }
    values.update(overrides)
    return coordination.create_assignment(**values)


def test_create_assignment_persists_minimum_contract(tmp_path):
    created = _create(tmp_path)

    assert created == {
        "assignment_id": created["assignment_id"],
        "project_key": str(tmp_path.resolve()),
        "assignment": "修复可靠发送失败后的补偿路径",
        "assignee": "codex-worker",
        "expected_reply": "提交 SHA、测试结果和剩余风险",
        "deadline": 2_000.0,
        "status": "assigned",
        "closed_at": None,
        "version": 1,
        "created_at": 1_000.0,
        "updated_at": 1_000.0,
    }
    assert created["assignment_id"].startswith("a-")
    assert coordination.get_assignment(created["assignment_id"]) == created


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_key", ""),
        ("assignment", "  "),
        ("assignee", ""),
    ],
)
def test_create_assignment_rejects_blank_required_text(tmp_path, field, value):
    with pytest.raises(ValueError, match=field):
        _create(tmp_path, **{field: value})


@pytest.mark.parametrize("deadline", [True, "tomorrow", math.inf, math.nan])
def test_create_assignment_rejects_invalid_deadline(tmp_path, deadline):
    with pytest.raises(ValueError, match="deadline"):
        _create(tmp_path, deadline=deadline)


def test_expected_reply_and_deadline_are_nullable(tmp_path):
    created = _create(tmp_path, expected_reply=None, deadline=None)

    assert created["expected_reply"] is None
    assert created["deadline"] is None

    epoch = _create(tmp_path, deadline=0)
    assert epoch["deadline"] == 0.0


def test_create_assignment_rejects_invalid_optional_reply(tmp_path):
    with pytest.raises(ValueError, match="expected_reply"):
        _create(tmp_path, expected_reply="  ")
    with pytest.raises(ValueError, match="expected_reply"):
        _create(tmp_path, expected_reply="x" * 2_001)


def test_assignment_lifecycle_and_explicit_close(tmp_path):
    assignment = _create(tmp_path)

    started = coordination.transition_assignment(
        assignment["assignment_id"],
        to_status="in_progress",
        expected_version=1,
        now=1_100.0,
    )
    assert (started["status"], started["version"], started["updated_at"]) == (
        "in_progress",
        2,
        1_100.0,
    )

    blocked = coordination.transition_assignment(
        assignment["assignment_id"],
        to_status="blocked",
        expected_version=2,
        now=1_200.0,
    )
    resumed = coordination.transition_assignment(
        assignment["assignment_id"],
        to_status="in_progress",
        expected_version=3,
        now=1_300.0,
    )
    review = coordination.transition_assignment(
        assignment["assignment_id"],
        to_status="review",
        expected_version=4,
        now=1_400.0,
    )

    assert blocked["version"] == 3
    assert resumed["version"] == 4
    assert review["version"] == 5
    assert review["closed_at"] is None

    closed = coordination.close_assignment(
        assignment["assignment_id"], expected_version=5, now=1_500.0,
    )
    assert closed["status"] == "closed"
    assert closed["closed_at"] == 1_500.0
    assert closed["updated_at"] == 1_500.0
    assert closed["version"] == 6


def test_review_can_return_to_in_progress(tmp_path):
    assignment = _create(tmp_path)
    for version, status in ((1, "in_progress"), (2, "review"), (3, "in_progress")):
        result = coordination.transition_assignment(
            assignment["assignment_id"],
            to_status=status,
            expected_version=version,
        )
    assert result["status"] == "in_progress"
    assert result["version"] == 4


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (None, "review"),
        (None, "closed"),
        ("in_progress", "assigned"),
        ("blocked", "review"),
    ],
)
def test_transition_assignment_rejects_illegal_transitions(tmp_path, start, target):
    assignment = _create(tmp_path)
    version = 1
    if start is not None:
        assignment = coordination.transition_assignment(
            assignment["assignment_id"],
            to_status=start,
            expected_version=version,
        )
        version = assignment["version"]

    with pytest.raises(ValueError, match="非法状态迁移|显式关闭"):
        coordination.transition_assignment(
            assignment["assignment_id"],
            to_status=target,
            expected_version=version,
        )


def test_stale_version_rejected_without_mutation(tmp_path):
    assignment = _create(tmp_path)

    with pytest.raises(ValueError, match="version 冲突"):
        coordination.transition_assignment(
            assignment["assignment_id"],
            to_status="in_progress",
            expected_version=99,
            now=1_100.0,
        )

    assert coordination.get_assignment(assignment["assignment_id"]) == assignment


def test_closed_assignment_rejects_further_mutation(tmp_path):
    assignment = _create(tmp_path)
    closed = coordination.close_assignment(
        assignment["assignment_id"], expected_version=1, now=1_100.0,
    )

    with pytest.raises(ValueError, match="已关闭"):
        coordination.transition_assignment(
            assignment["assignment_id"],
            to_status="in_progress",
            expected_version=closed["version"],
        )
    with pytest.raises(ValueError, match="已关闭"):
        coordination.close_assignment(
            assignment["assignment_id"], expected_version=closed["version"],
        )


def test_transition_rejects_missing_assignment_and_bad_version(tmp_path):
    _create(tmp_path)

    with pytest.raises(ValueError, match="任务不存在"):
        coordination.transition_assignment(
            "a-missing", to_status="in_progress", expected_version=1,
        )
    for version in (None, True, 0, "1"):
        with pytest.raises(ValueError, match="expected_version"):
            coordination.transition_assignment(
                "a-missing", to_status="in_progress", expected_version=version,
            )


def test_list_assignments_filters_project_status_and_assignee(tmp_path):
    other_project = tmp_path / "other"
    first = _create(tmp_path, assignee="codex-worker", deadline=3_000.0)
    second = _create(tmp_path, assignee="claude-reviewer", deadline=2_500.0)
    _create(other_project, assignee="codex-worker")
    coordination.transition_assignment(
        second["assignment_id"], to_status="in_progress", expected_version=1,
    )

    assert [row["assignment_id"] for row in coordination.list_assignments(str(tmp_path))] == [
        second["assignment_id"],
        first["assignment_id"],
    ]
    assert coordination.list_assignments(
        str(tmp_path), statuses=["in_progress"], assignee="claude-reviewer",
    ) == [coordination.get_assignment(second["assignment_id"])]
    with pytest.raises(ValueError, match="非法 status"):
        coordination.list_assignments(str(tmp_path), statuses=["unknown"])


def test_create_assignment_rejects_overlong_texts(tmp_path):
    with pytest.raises(ValueError, match="assignment"):
        _create(tmp_path, assignment="x" * (coordination.ASSIGNMENT_TEXT_LIMIT + 1))
    with pytest.raises(ValueError, match="assignee"):
        _create(tmp_path, assignee="y" * (coordination.ASSIGNEE_TEXT_LIMIT + 1))


def test_concurrent_transitions_same_version_exactly_one_wins(tmp_path):
    import threading

    assignment = _create(tmp_path)
    results = {}
    barrier = threading.Barrier(2)

    def worker(name):
        barrier.wait()
        try:
            results[name] = coordination.transition_assignment(
                assignment["assignment_id"],
                to_status="in_progress",
                expected_version=1,
            )
        except ValueError as exc:
            results[name] = exc

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = [r for r in results.values() if isinstance(r, dict)]
    failed = [r for r in results.values() if isinstance(r, ValueError)]
    assert len(ok) == 1 and len(failed) == 1
    assert ok[0]["version"] == 2
    assert "version 冲突" in str(failed[0]) or "非法状态迁移" in str(failed[0])
    assert coordination.get_assignment(assignment["assignment_id"])["version"] == 2


def test_list_assignments_stable_order_deadline_then_id(tmp_path):
    same_deadline = [
        _create(tmp_path, deadline=2_000.0, assignee=f"w-{i}", now=1_000.0 + i)
        for i in range(3)
    ]
    later = _create(tmp_path, deadline=2_500.0, now=1_100.0)
    rows = coordination.list_assignments(str(tmp_path))
    assert [r["assignment_id"] for r in rows] == [
        *[a["assignment_id"] for a in same_deadline],
        later["assignment_id"],
    ]


def test_project_key_canonical_on_create_and_list(tmp_path):
    sub = tmp_path / "proj"
    sub.mkdir()
    created = coordination.create_assignment(
        project_key=f"{sub}{os.sep}.",
        assignment="canonical 校验",
        assignee="w",
        now=1_000.0,
    )
    assert created["project_key"] == str(sub.resolve())
    assert [
        r["assignment_id"]
        for r in coordination.list_assignments(f"{sub}{os.sep}.")
    ] == [created["assignment_id"]]


@pytest.mark.parametrize("bad_now", [True, False, math.nan, math.inf, "1000"])
def test_explicit_now_rejects_non_finite_epoch(tmp_path, bad_now):
    with pytest.raises(ValueError, match="now"):
        _create(tmp_path, now=bad_now)
    assignment = _create(tmp_path)
    with pytest.raises(ValueError, match="now"):
        coordination.transition_assignment(
            assignment["assignment_id"],
            to_status="in_progress", expected_version=1, now=bad_now,
        )
    with pytest.raises(ValueError, match="now"):
        coordination.close_assignment(
            assignment["assignment_id"], expected_version=1, now=bad_now,
        )


def test_list_assignments_empty_statuses_returns_empty(tmp_path):
    _create(tmp_path)
    assert coordination.list_assignments(str(tmp_path), statuses=[]) == []


def test_successful_cas_returns_exactly_expected_plus_one(tmp_path):
    assignment = _create(tmp_path)
    for expected in (1, 2, 3):
        result = coordination.transition_assignment(
            assignment["assignment_id"],
            to_status="in_progress" if expected % 2 else "blocked",
            expected_version=expected,
        )
        assert result["version"] == expected + 1
        assert result == coordination.get_assignment(assignment["assignment_id"])


def test_transition_from_status_interleaving_toctou(tmp_path):
    """Lead REVIEW_BLOCK 复现：from-status 判定后另一写者抢先迁移，
    持新 expected_version 的调用不得把非法 from-status 写成功。"""
    assignment = _create(tmp_path)
    aid = assignment["assignment_id"]
    coordination.transition_assignment(
        aid, to_status="in_progress", expected_version=1,
    )
    # writer A 读到 in_progress/v2，判定 →review 合法，尚未进入 CAS
    # writer B 抢先：in_progress→blocked（v3）
    coordination.transition_assignment(aid, to_status="blocked", expected_version=2)
    # writer A 携 expected_version=3 继续：blocked→review 非法，必须拒绝零变更
    with pytest.raises(ValueError, match="非法状态迁移"):
        coordination.transition_assignment(
            aid, to_status="review", expected_version=3,
        )
    row = coordination.get_assignment(aid)
    assert (row["status"], row["version"]) == ("blocked", 3)


def test_concurrent_divergent_transitions_final_state_consistent(tmp_path):
    """两并发写者从 in_progress/v2 竞争 blocked 与 review：恰一成功，
    终态必须是合法迁移结果，败者报 version 冲突或非法迁移。"""
    import threading

    assignment = _create(tmp_path)
    aid = assignment["assignment_id"]
    coordination.transition_assignment(
        aid, to_status="in_progress", expected_version=1,
    )
    results = {}
    barrier = threading.Barrier(2)

    def worker(name, target):
        barrier.wait()
        try:
            results[name] = coordination.transition_assignment(
                aid, to_status=target, expected_version=2,
            )
        except ValueError as exc:
            results[name] = exc

    threads = [
        threading.Thread(target=worker, args=("w-blocked", "blocked")),
        threading.Thread(target=worker, args=("w-review", "review")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = [r for r in results.values() if isinstance(r, dict)]
    failed = [r for r in results.values() if isinstance(r, ValueError)]
    assert len(ok) == 1 and len(failed) == 1
    assert ok[0]["version"] == 3
    assert ok[0]["status"] in ("blocked", "review")
    assert "冲突" in str(failed[0]) or "非法状态迁移" in str(failed[0])
    final = coordination.get_assignment(aid)
    assert (final["status"], final["version"]) == (ok[0]["status"], 3)
