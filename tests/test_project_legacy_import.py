"""PROJ-003 legacy importer — ordinary functional tests.

Covers frozen source key/digest vectors, source availability separation (not
false-empty), exact-path grouping (no heuristic merge), per-candidate single
Store call, idempotent replay, and read-only authority immutability.

These are ordinary functional/regression tests only. Adversarial/security
harnesses are owned by separate reviewers (Claude/Kimi).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from agent_cockpit import project_legacy_import as pli
from agent_cockpit import project_registry_store


# ── canonicalization helpers (mirror module; vectors assert exact sha256) ──
def _cj(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _uri(v: Any) -> str:
    return "sha256:" + hashlib.sha256(_cj(v).encode("ascii")).hexdigest()


# ── fixture builders ───────────────────────────────────────────────────────
def _make_agent_mail_db(tmp_path: Path, projects: list[dict[str, Any]]) -> Path:
    db = tmp_path / "agent-mail.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE projects (id INTEGER NOT NULL PRIMARY KEY, "
        "slug VARCHAR(255) NOT NULL, human_key VARCHAR(255) NOT NULL, "
        "created_at DATETIME NOT NULL, archived_at DATETIME)"
    )
    for p in projects:
        con.execute(
            "INSERT INTO projects (id, slug, human_key, created_at, archived_at) VALUES (?,?,?,?,?)",
            (p["id"], p.get("slug", ""), p["human_key"], "2026-01-01", p.get("archived_at")),
        )
    con.commit()
    con.close()
    return db


def _make_mail_projects(tmp_path: Path, sessions: dict[str, Any] | None) -> Path:
    path = tmp_path / "mail-projects.json"
    data = {"version": 1, "sessions": sessions or {}}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _herdr_v3_workspace(workspace_id: str, identity_cwd: str) -> dict[str, Any]:
    return {
        "id": workspace_id,
        "custom_name": "Workspace",
        "identity_cwd": identity_cwd,
        "public_pane_numbers": {"pane-1": 1},
        "next_public_pane_number": 2,
        "public_tab_numbers": [1],
        "next_public_tab_number": 2,
        "tabs": [],
        "active_tab": 0,
    }


def _make_herdr_session(tmp_path: Path, session: str, _session_dir: str,
                        workspaces: list[dict[str, str]], version: int = 3) -> Path:
    directory = tmp_path / "herdr-sessions"
    directory.mkdir(exist_ok=True)
    session_directory = directory / session
    session_directory.mkdir(exist_ok=True)
    persisted_workspaces = [
        _herdr_v3_workspace(w["workspace_id"], w["identity_cwd"])
        for w in workspaces
    ]
    (session_directory / "session.json").write_text(
        json.dumps(
            {
                "version": version,
                "workspaces": persisted_workspaces,
                "active": 0,
                "selected": 0,
                "sidebar_width": 240,
                "sidebar_section_split": 0.5,
                "collapsed_space_keys": [],
            }
        ),
        encoding="utf-8",
    )
    return directory


def _make_coordination_db(tmp_path: Path, runs: list[dict[str, Any]]) -> Path:
    db = tmp_path / "coordination.sqlite3"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, project_key TEXT NOT NULL, "
        "session TEXT NOT NULL, session_dir TEXT NOT NULL, revision INTEGER NOT NULL, "
        "state TEXT NOT NULL, config_hash TEXT NOT NULL, started_ts REAL NOT NULL, "
        "closed_ts REAL, UNIQUE(session, session_dir, revision))"
    )
    for r in runs:
        con.execute(
            "INSERT INTO runs (run_id, project_key, session, session_dir, revision, state, "
            "config_hash, started_ts) VALUES (?,?,?,?,?,?,?,?)",
            (r["run_id"], r["project_key"], r["session"], r["session_dir"],
             r["revision"], "running", "fixture-config", 1.0),
        )
    con.commit()
    con.close()
    return db


def _registry(tmp_path: Path) -> project_registry_store.ProjectRegistryStore:
    return project_registry_store.initialize(tmp_path / "registry.sqlite3")


ALPHA = "/srv/repos/alpha"
HERDR_SD = "/var/lib/herdr/sessions/alpha-dev"


def _full_roots(tmp_path: Path, *, boundary: Path | None = None) -> pli.LegacyRoots:
    am = _make_agent_mail_db(tmp_path, [{"id": 17, "slug": "alpha", "human_key": ALPHA}])
    herdr_session_dir = str(tmp_path / "herdr-sessions" / "alpha-dev")
    mp = _make_mail_projects(
        tmp_path,
        {"alpha-dev": {"session_dir": herdr_session_dir, "project": ALPHA}},
    )
    hr = _make_herdr_session(
        tmp_path, "alpha-dev", HERDR_SD,
        [{"workspace_id": "w1", "identity_cwd": ALPHA}],
    )
    cr = _make_coordination_db(
        tmp_path,
        [{"run_id": "run_01JABCDEF0123456789ABCDEF", "project_key": ALPHA,
          "session": "alpha-dev", "session_dir": herdr_session_dir, "revision": 1}],
    )
    return pli.LegacyRoots(
        agent_mail_db=am, mail_projects_json=mp, herdr_sessions_dir=hr,
        coordination_db=cr, local_boundary=boundary,
    )


# ── 1. Frozen source key/digest vectors (exact sha256) ─────────────────────
@pytest.mark.parametrize(
    "kind,identity,evidence,exp_key,exp_digest",
    [
        ("agent_mail_project", {"project_id": 17},
         {"human_key": ALPHA, "project_id": 17, "slug": "alpha"},
         "sha256:3828c1823d312bcc3ea5b764145c1377382a66f0c8b584532f4b59dd8cacd7dd",
         "sha256:f7857aef699e2e7fbcb660580313fdbe5a96ec74b1ffda197711fa5f5ba56bca"),
        ("mail_projects_session",
         {"session": "alpha-dev", "session_dir": HERDR_SD},
         {"project": ALPHA, "session": "alpha-dev", "session_dir": HERDR_SD, "version": 1},
         "sha256:8b9e1709d343538ad9f36d9d7f3c658c16e3de6082d9cb17381855ddba2b79a1",
         "sha256:a2785b872af6e8f80ea2cfe68a26062d5e22d57e7fe11e9b571b609cf268a32e"),
        ("herdr_session",
         {"session": "alpha-dev", "session_dir": HERDR_SD},
         {"session": "alpha-dev", "session_dir": HERDR_SD, "version": 3,
          "workspaces": [{"identity_cwd": ALPHA, "workspace_id": "w1"}]},
         "sha256:8b9e1709d343538ad9f36d9d7f3c658c16e3de6082d9cb17381855ddba2b79a1",
         "sha256:a36335540a8a1bb64e6025413f2a60bfcbf0e0516eca760d3dae06589e8df5a3"),
        ("coordination_run", {"run_id": "run_01JABCDEF0123456789ABCDEF"},
         {"project_key": ALPHA, "revision": 1, "run_id": "run_01JABCDEF0123456789ABCDEF",
          "session": "alpha-dev", "session_dir": HERDR_SD},
         "sha256:1c24ff94f97324aca330b3fdb1d351d31fef8ed6ae35f0fd1297724ab60980c2",
         "sha256:a84c9cab11d928d77ddca70de109575dc339118b6172787acbef19817dfd763c"),
    ],
)
def test_source_identity_and_digest_vectors(kind, identity, evidence, exp_key, exp_digest):
    assert pli.canonical_json({"a": 1, "b": 2}) == '{"a":1,"b":2}'
    assert _uri(identity) == exp_key
    assert _uri(evidence) == exp_digest


def test_digest_ordering_invariants():
    # H01: key order independent
    assert _uri({"project_id": 17, "slug": "alpha", "human_key": ALPHA}) == _uri(
        {"human_key": ALPHA, "project_id": 17, "slug": "alpha"}
    )
    # H03: reverse herdr workspace enumeration → same digest
    w1 = [{"identity_cwd": ALPHA, "workspace_id": "w1"}]
    assert _uri({"session": "alpha-dev", "session_dir": HERDR_SD, "version": 3, "workspaces": w1}) == _uri(
        {"workspaces": list(reversed(w1)), "version": 3, "session_dir": HERDR_SD, "session": "alpha-dev"}
    )


def test_canonical_json_rejects_non_string_mapping_keys():
    with pytest.raises(ValueError):
        pli.canonical_json({1: "value"})


# ── 2. Reader round-trip + availability separation ─────────────────────────
def test_agent_mail_reader_reads_unarchived_only(tmp_path):
    db = _make_agent_mail_db(
        tmp_path,
        [{"id": 1, "slug": "a", "human_key": "/srv/a"}, {"id": 2, "slug": "b", "human_key": "/srv/b", "archived_at": "2026"}],
    )
    roots = pli.LegacyRoots(agent_mail_db=db)
    res = pli.AgentMailProjectReader().read(roots)
    assert res.state == "ok"
    pids = [r["project_id"] for r in res.records]
    assert pids == [1]  # archived excluded


def test_source_unavailable_is_not_false_empty(tmp_path):
    # A02: only agent mail valid, others missing → one candidate, others unavailable
    am = _make_agent_mail_db(tmp_path, [{"id": 17, "slug": "alpha", "human_key": ALPHA}])
    roots = pli.LegacyRoots(agent_mail_db=am)  # others None
    report = pli.import_legacy(_registry(tmp_path), roots)
    states = {s.kind: s.state for s in report.sources}
    assert states["agent_mail_project"] == "ok"
    assert states["mail_projects_session"] == "unavailable"
    assert states["herdr_session"] == "unavailable"
    assert states["coordination_run"] == "unavailable"
    assert any(o.state in ("committed", "replayed") for o in report.candidates)


def test_all_sources_empty_valid_is_not_unavailable(tmp_path):
    # A06: all valid-empty → zero candidates, all ok (not unavailable)
    am = _make_agent_mail_db(tmp_path, [])
    mp = _make_mail_projects(tmp_path, {})
    hr_dir = tmp_path / "herdr-sessions"
    hr_dir.mkdir()
    cr = _make_coordination_db(tmp_path, [])
    roots = pli.LegacyRoots(agent_mail_db=am, mail_projects_json=mp,
                            herdr_sessions_dir=hr_dir, coordination_db=cr)
    report = pli.import_legacy(_registry(tmp_path), roots)
    assert all(s.state == "ok" for s in report.sources)
    assert report.candidates == ()


def test_corrupt_source_is_error_and_blocks_completeness(tmp_path):
    # A07/A08: malformed JSON mail → error, complete False
    am = _make_agent_mail_db(tmp_path, [{"id": 17, "slug": "alpha", "human_key": ALPHA}])
    bad = tmp_path / "mail-projects.json"
    bad.write_text("{not valid json", encoding="utf-8")
    roots = pli.LegacyRoots(agent_mail_db=am, mail_projects_json=bad)
    report = pli.import_legacy(_registry(tmp_path), roots)
    mail = next(s for s in report.sources if s.kind == "mail_projects_session")
    assert mail.state == "error"
    assert report.complete is False


def test_future_schema_source_is_error(tmp_path):
    # A12: unknown mail version → error
    am = _make_agent_mail_db(tmp_path, [{"id": 17, "slug": "alpha", "human_key": ALPHA}])
    mp = tmp_path / "mail-projects.json"
    mp.write_text(json.dumps({"version": 99, "sessions": {}}), encoding="utf-8")
    roots = pli.LegacyRoots(agent_mail_db=am, mail_projects_json=mp)
    report = pli.import_legacy(_registry(tmp_path), roots)
    assert next(s for s in report.sources if s.kind == "mail_projects_session").state == "error"


@pytest.mark.parametrize(
    "payload",
    [
        {"version": True, "sessions": {}},
        {"version": 1, "sessions": []},
        {"version": 1, "sessions": {}, "future": "field"},
    ],
)
def test_mail_projects_reader_rejects_non_exact_root_schema(tmp_path, payload):
    path = tmp_path / "mail-projects.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = pli.MailProjectsJsonReader().read(pli.LegacyRoots(mail_projects_json=path))
    assert result.state == "error"
    assert result.detail_code == pli.SOURCE_CORRUPT


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 99, "workspaces": [], "active": 0, "selected": 0,
         "sidebar_width": 240, "sidebar_section_split": 0.5,
         "collapsed_space_keys": []},
        {"version": True, "workspaces": [], "active": 0, "selected": 0,
         "sidebar_width": 240, "sidebar_section_split": 0.5,
         "collapsed_space_keys": []},
        {"version": 3, "workspaces": [], "active": 0, "selected": 0,
         "sidebar_width": 240, "sidebar_section_split": 0.5,
         "collapsed_space_keys": [], "future": "field"},
    ],
)
def test_herdr_reader_rejects_non_exact_root_schema(tmp_path, payload):
    directory = tmp_path / "herdr-sessions"
    descriptor_dir = directory / "alpha-dev"
    descriptor_dir.mkdir(parents=True)
    (descriptor_dir / "session.json").write_text(json.dumps(payload), encoding="utf-8")
    result = pli.HerdrSessionReader().read(pli.LegacyRoots(herdr_sessions_dir=directory))
    assert result.state == "error"
    assert result.detail_code == pli.SOURCE_CORRUPT


def test_herdr_reader_accepts_real_v3_persisted_shape_and_normalizes_evidence(tmp_path):
    directory = _make_herdr_session(
        tmp_path, "alpha-dev", "ignored-by-authority-scanner",
        [{"workspace_id": "workspace-1", "identity_cwd": ALPHA}],
    )

    result = pli.HerdrSessionReader().read(
        pli.LegacyRoots(herdr_sessions_dir=directory)
    )

    assert result.state == "ok"
    assert result.records == ({
        "session": "alpha-dev",
        "session_dir": str(directory / "alpha-dev"),
        "version": 3,
        "workspaces": ({
            "workspace_id": "workspace-1",
            "identity_cwd": ALPHA,
        },),
    },)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda workspace: workspace.update({"id": ""}),
        lambda workspace: workspace.update({"workspace_id": workspace.pop("id")}),
        lambda workspace: workspace.update({"future": "field"}),
        lambda workspace: workspace.update({"next_public_pane_number": True}),
        lambda workspace: workspace.update({"public_tab_numbers": [True]}),
        lambda workspace: workspace.update({"tabs": {}}),
    ],
)
def test_herdr_reader_rejects_non_exact_persisted_workspace_schema(
    tmp_path, mutation,
):
    directory = _make_herdr_session(
        tmp_path, "alpha-dev", "ignored-by-authority-scanner",
        [{"workspace_id": "workspace-1", "identity_cwd": ALPHA}],
    )
    descriptor = directory / "alpha-dev" / "session.json"
    payload = json.loads(descriptor.read_text(encoding="utf-8"))
    mutation(payload["workspaces"][0])
    descriptor.write_text(json.dumps(payload), encoding="utf-8")

    result = pli.HerdrSessionReader().read(
        pli.LegacyRoots(herdr_sessions_dir=directory)
    )

    assert result.state == "error"
    assert result.detail_code == pli.SOURCE_CORRUPT


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE TABLE projects (id INTEGER NOT NULL PRIMARY KEY, "
        "slug VARCHAR(255) NOT NULL, human_key VARCHAR(255) NOT NULL, "
        "created_at DATETIME NOT NULL, archived_at DATETIME, future TEXT)",
        "CREATE TABLE projects (id INTEGER NOT NULL PRIMARY KEY, "
        "slug VARCHAR(255) NOT NULL, human_key VARCHAR(255) NOT NULL, "
        "archived_at DATETIME)",
        "CREATE TABLE projects (id INTEGER NOT NULL PRIMARY KEY, "
        "slug VARCHAR(255), human_key VARCHAR(255) NOT NULL, "
        "created_at DATETIME NOT NULL, archived_at DATETIME)",
    ],
)
def test_agent_mail_reader_rejects_non_exact_table_fingerprint(tmp_path, ddl):
    db = tmp_path / "agent-mail-invalid.db"
    con = sqlite3.connect(db)
    con.execute(ddl)
    con.close()
    result = pli.AgentMailProjectReader().read(pli.LegacyRoots(agent_mail_db=db))
    assert result.state == "error"
    assert result.detail_code == pli.SOURCE_CORRUPT


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, project_key TEXT NOT NULL, "
        "session TEXT NOT NULL, session_dir TEXT NOT NULL, revision INTEGER NOT NULL, "
        "state TEXT NOT NULL, config_hash TEXT NOT NULL, started_ts REAL NOT NULL, "
        "closed_ts REAL, future TEXT)",
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, project_key TEXT NOT NULL, "
        "session TEXT NOT NULL, session_dir TEXT NOT NULL, revision INTEGER NOT NULL, "
        "state TEXT NOT NULL, config_hash TEXT NOT NULL, started_ts REAL NOT NULL)",
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, project_key TEXT, "
        "session TEXT NOT NULL, session_dir TEXT NOT NULL, revision INTEGER NOT NULL, "
        "state TEXT NOT NULL, config_hash TEXT NOT NULL, started_ts REAL NOT NULL, "
        "closed_ts REAL)",
    ],
)
def test_coordination_reader_rejects_non_exact_table_fingerprint(tmp_path, ddl):
    db = tmp_path / "coordination-invalid.sqlite3"
    con = sqlite3.connect(db)
    con.execute(ddl)
    con.close()
    result = pli.CoordinationRunReader().read(pli.LegacyRoots(coordination_db=db))
    assert result.state == "error"
    assert result.detail_code == pli.SOURCE_CORRUPT


# ── 3. Exact-path grouping, no heuristic merge ─────────────────────────────
def test_full_evidence_converges_to_one_candidate_four_bindings(tmp_path):
    # A01: all four sources agree on path → one candidate, one Store call, 4 bindings
    store = _registry(tmp_path)
    report = pli.import_legacy(store, _full_roots(tmp_path))
    assert len(report.candidates) == 1
    outcome = report.candidates[0]
    assert outcome.state == "committed"
    assert outcome.canonical_path == ALPHA
    con = sqlite3.connect(f"file:{store.path}?mode=ro", uri=True)
    kinds = sorted(b[0] for b in con.execute(
        "SELECT source_kind FROM legacy_project_bindings").fetchall())
    con.close()
    assert kinds == ["agent_mail_project", "coordination_run", "herdr_session", "mail_projects_session"]


def _capture_import(
    tmp_path: Path, results: list[pli.SourceReadResult], *, reverse: bool = False,
) -> tuple[pli.ImportReport, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    class Reader:
        def __init__(self, result: pli.SourceReadResult) -> None:
            self.result = result
            self.kind = result.kind

        def read(self, roots: pli.LegacyRoots) -> pli.SourceReadResult:
            return self.result

    class Store:
        def import_legacy_project(self, **kwargs):
            calls.append(kwargs)
            return type("Result", (), {"replayed": False})()

    ordered = list(reversed(results)) if reverse else results
    readers = [Reader(result) for result in ordered]
    report = pli.import_legacy(Store(), pli.LegacyRoots(), readers=readers)  # type: ignore[arg-type]
    return report, calls


def test_two_mail_sessions_on_one_path_preserve_stable_bindings(tmp_path):
    records = (
        {"session": "alpha-a", "session_dir": "/var/lib/herdr/sessions/alpha-a",
         "project": ALPHA, "version": 1},
        {"session": "alpha-b", "session_dir": "/var/lib/herdr/sessions/alpha-b",
         "project": ALPHA, "version": 1},
    )
    forward = pli.SourceReadResult("mail_projects_session", "ok", records)
    backward = pli.SourceReadResult("mail_projects_session", "ok", tuple(reversed(records)))

    forward_report, forward_calls = _capture_import(tmp_path, [forward])
    backward_report, backward_calls = _capture_import(tmp_path, [backward])

    assert forward_report == backward_report
    assert forward_calls == backward_calls
    assert len(forward_calls) == 1
    assert len(forward_calls[0]["sources"]) == 2
    assert {source.source_kind for source in forward_calls[0]["sources"]} == {
        "mail_projects_session"
    }


def test_two_agent_mail_projects_on_one_path_preserve_stable_bindings(tmp_path):
    records = (
        {"project_id": 17, "slug": "alpha-primary", "human_key": ALPHA},
        {"project_id": 18, "slug": "alpha-secondary", "human_key": ALPHA},
    )
    forward = pli.SourceReadResult("agent_mail_project", "ok", records)
    backward = pli.SourceReadResult("agent_mail_project", "ok", tuple(reversed(records)))

    forward_report, forward_calls = _capture_import(tmp_path, [forward])
    backward_report, backward_calls = _capture_import(tmp_path, [backward])

    assert forward_report == backward_report
    assert forward_calls == backward_calls
    assert len(forward_calls) == 1
    assert len(forward_calls[0]["sources"]) == 2
    assert {source.source_kind for source in forward_calls[0]["sources"]} == {
        "agent_mail_project"
    }


def test_two_coordination_runs_on_one_path_preserve_stable_bindings(tmp_path):
    records = (
        {"run_id": "run_alpha_a", "project_key": ALPHA, "session": "alpha-dev",
         "session_dir": HERDR_SD, "revision": 1},
        {"run_id": "run_alpha_b", "project_key": ALPHA, "session": "alpha-dev",
         "session_dir": HERDR_SD, "revision": 2},
    )
    forward = pli.SourceReadResult("coordination_run", "ok", records)
    backward = pli.SourceReadResult("coordination_run", "ok", tuple(reversed(records)))

    forward_report, forward_calls = _capture_import(tmp_path, [forward])
    backward_report, backward_calls = _capture_import(tmp_path, [backward])

    assert forward_report == backward_report
    assert forward_calls == backward_calls
    assert len(forward_calls) == 1
    assert len(forward_calls[0]["sources"]) == 2
    assert {source.source_kind for source in forward_calls[0]["sources"]} == {
        "coordination_run"
    }


def test_two_herdr_sessions_on_one_candidate_preserve_stable_bindings(tmp_path):
    owner = pli.SourceReadResult(
        "agent_mail_project", "ok",
        ({"project_id": 17, "slug": "alpha", "human_key": ALPHA},),
    )
    records = (
        {"session": "alpha-a", "session_dir": "/var/lib/herdr/sessions/alpha-a",
         "version": 3,
         "workspaces": ({"workspace_id": "w1", "identity_cwd": ALPHA},)},
        {"session": "alpha-b", "session_dir": "/var/lib/herdr/sessions/alpha-b",
         "version": 3,
         "workspaces": ({"workspace_id": "w2", "identity_cwd": ALPHA},)},
    )
    forward = pli.SourceReadResult("herdr_session", "ok", records)
    backward = pli.SourceReadResult("herdr_session", "ok", tuple(reversed(records)))

    forward_report, forward_calls = _capture_import(tmp_path, [owner, forward])
    backward_report, backward_calls = _capture_import(tmp_path, [owner, backward])

    assert forward_report == backward_report
    assert forward_calls == backward_calls
    assert len(forward_calls) == 1
    assert len(forward_calls[0]["sources"]) == 3
    assert sum(
        source.source_kind == "herdr_session"
        for source in forward_calls[0]["sources"]
    ) == 2


def test_source_reader_enumeration_order_is_stable(tmp_path):
    results = [
        pli.SourceReadResult(
            "agent_mail_project", "ok",
            ({"project_id": 17, "slug": "alpha", "human_key": ALPHA},),
        ),
        pli.SourceReadResult(
            "mail_projects_session", "ok",
            ({"session": "alpha-dev", "session_dir": HERDR_SD,
              "project": ALPHA, "version": 1},),
        ),
        pli.SourceReadResult(
            "coordination_run", "ok",
            ({"run_id": "run_alpha", "project_key": ALPHA,
              "session": "alpha-dev", "session_dir": HERDR_SD, "revision": 1},),
        ),
    ]

    forward_report, forward_calls = _capture_import(tmp_path, results)
    backward_report, backward_calls = _capture_import(tmp_path, results, reverse=True)

    assert forward_report == backward_report
    assert forward_calls == backward_calls


def test_same_source_identity_with_changed_digest_fails_closed_stably(tmp_path):
    records = (
        {"project_id": 17, "slug": "alpha", "human_key": ALPHA},
        {"project_id": 17, "slug": "renamed-alpha", "human_key": ALPHA},
    )
    forward = pli.SourceReadResult("agent_mail_project", "ok", records)
    backward = pli.SourceReadResult("agent_mail_project", "ok", tuple(reversed(records)))

    forward_report, forward_calls = _capture_import(tmp_path, [forward])
    backward_report, backward_calls = _capture_import(tmp_path, [backward])

    assert forward_calls == backward_calls == []
    assert forward_report == backward_report
    status = next(s for s in forward_report.sources if s.kind == "agent_mail_project")
    assert status == pli.SourceStatus("agent_mail_project", "error", pli.EVIDENCE_CONFLICT)
    assert forward_report.complete is False


def test_same_source_identity_and_digest_is_idempotent(tmp_path):
    record = {"project_id": 17, "slug": "alpha", "human_key": ALPHA}
    source = pli.SourceReadResult("agent_mail_project", "ok", (record, dict(record)))

    report, calls = _capture_import(tmp_path, [source])

    assert report.complete is True
    assert len(calls) == 1
    assert len(calls[0]["sources"]) == 1


def test_mail_coordination_generation_disagreement_fails_closed(tmp_path):
    mail = pli.SourceReadResult(
        "mail_projects_session", "ok",
        ({"session": "shared", "session_dir": HERDR_SD,
          "project": ALPHA, "version": 1},),
    )
    coordination = pli.SourceReadResult(
        "coordination_run", "ok",
        ({"run_id": "run_shared", "project_key": "/srv/repos/beta",
          "session": "shared", "session_dir": HERDR_SD, "revision": 1},),
    )
    herdr = pli.SourceReadResult(
        "herdr_session", "ok",
        ({"session": "shared", "session_dir": HERDR_SD, "version": 3,
          "workspaces": ({"workspace_id": "w1", "identity_cwd": ALPHA},)},),
    )

    report, calls = _capture_import(tmp_path, [mail, coordination, herdr])
    reverse_report, reverse_calls = _capture_import(
        tmp_path, [mail, coordination, herdr], reverse=True,
    )

    assert calls == reverse_calls == []
    assert report == reverse_report
    assert report.candidates == ()
    assert report.complete is False
    statuses = {status.kind: status for status in report.sources}
    assert statuses["mail_projects_session"].detail_code == pli.AUTHORITY_DISAGREEMENT
    assert statuses["coordination_run"].detail_code == pli.AUTHORITY_DISAGREEMENT
    assert statuses["mail_projects_session"].state == "error"
    assert statuses["coordination_run"].state == "error"


def test_herdr_generation_disagreement_fails_closed(tmp_path):
    mail = pli.SourceReadResult(
        "mail_projects_session", "ok",
        ({"session": "shared", "session_dir": HERDR_SD,
          "project": ALPHA, "version": 1},),
    )
    coordination = pli.SourceReadResult(
        "coordination_run", "ok",
        ({"run_id": "run_shared", "project_key": ALPHA,
          "session": "shared", "session_dir": HERDR_SD, "revision": 1},),
    )
    herdr = pli.SourceReadResult(
        "herdr_session", "ok",
        ({"session": "shared", "session_dir": HERDR_SD, "version": 3,
          "workspaces": ({"workspace_id": "w1", "identity_cwd": "/srv/repos/beta"},)},),
    )

    report, calls = _capture_import(tmp_path, [mail, coordination, herdr])

    assert calls == []
    assert report.candidates == ()
    assert report.complete is False
    statuses = {status.kind: status for status in report.sources}
    for kind in ("mail_projects_session", "coordination_run", "herdr_session"):
        assert statuses[kind].state == "error"
        assert statuses[kind].detail_code == pli.AUTHORITY_DISAGREEMENT


def test_no_merge_by_basename_or_session(tmp_path):
    # two distinct paths, same basename/session → two candidates, never merged
    am = _make_agent_mail_db(
        tmp_path,
        [{"id": 1, "slug": "a", "human_key": "/srv/x/alpha"}, {"id": 2, "slug": "b", "human_key": "/srv/y/alpha"}],
    )
    roots = pli.LegacyRoots(agent_mail_db=am)
    report = pli.import_legacy(_registry(tmp_path), roots)
    paths = sorted(o.canonical_path for o in report.candidates)
    assert paths == ["/srv/x/alpha", "/srv/y/alpha"]


def test_herdr_alone_creates_no_candidate(tmp_path):
    # A04: Herdr observational only
    hr = _make_herdr_session(tmp_path, "alpha-dev", HERDR_SD,
                             [{"workspace_id": "w1", "identity_cwd": ALPHA}])
    roots = pli.LegacyRoots(herdr_sessions_dir=hr)
    report = pli.import_legacy(_registry(tmp_path), roots)
    assert report.candidates == ()


def test_herdr_session_spanning_multiple_paths_attaches_to_neither(tmp_path):
    # H09: do not pick first
    am = _make_agent_mail_db(
        tmp_path,
        [{"id": 1, "slug": "a", "human_key": "/srv/alpha"}, {"id": 2, "slug": "b", "human_key": "/srv/beta"}],
    )
    hr = _make_herdr_session(tmp_path, "multi", "/var/lib/herdr/sessions/multi",
                             [{"workspace_id": "w1", "identity_cwd": "/srv/alpha"},
                              {"workspace_id": "w2", "identity_cwd": "/srv/beta"}])
    roots = pli.LegacyRoots(agent_mail_db=am, herdr_sessions_dir=hr)
    store = _registry(tmp_path)
    report = pli.import_legacy(store, roots)
    # no herdr binding should have been attached (ambiguous)
    con = sqlite3.connect(f"file:{store.path}?mode=ro", uri=True)
    herdr = con.execute(
        "SELECT COUNT(*) FROM legacy_project_bindings WHERE source_kind='herdr_session'"
    ).fetchone()[0]
    con.close()
    assert herdr == 0


# ── 4. Per-candidate single Store call + replay ────────────────────────────
def test_one_store_call_per_candidate(tmp_path):
    am = _make_agent_mail_db(
        tmp_path,
        [{"id": 1, "slug": "a", "human_key": "/srv/a"}, {"id": 2, "slug": "b", "human_key": "/srv/b"}],
    )
    roots = pli.LegacyRoots(agent_mail_db=am)
    calls: list[dict[str, Any]] = []

    class _Result:
        replayed = False

    class OKStore:
        def import_legacy_project(self, **kw):
            calls.append(kw)
            return _Result()

    pli.import_legacy(OKStore(), roots)  # type: ignore[arg-type]
    assert len(calls) == 2
    paths = sorted(c["canonical_path"] for c in calls)
    assert paths == ["/srv/a", "/srv/b"]
    for call in calls:
        for s in call["sources"]:
            assert s.source_key.startswith("sha256:") and len(s.source_key) == 71
            assert s.source_digest.startswith("sha256:")


def test_idempotent_replay_returns_replayed_and_no_duplicate(tmp_path):
    store = _registry(tmp_path)
    roots = _full_roots(tmp_path)
    r1 = pli.import_legacy(store, roots)
    r2 = pli.import_legacy(store, roots)
    assert r1.candidates[0].state == "committed"
    assert r2.candidates[0].state == "replayed"
    assert r2.candidates[0].replayed is True
    con = sqlite3.connect(f"file:{store.path}?mode=ro", uri=True)
    n_projects = con.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    n_bindings = con.execute("SELECT COUNT(*) FROM legacy_project_bindings").fetchone()[0]
    con.close()
    assert n_projects == 1
    assert n_bindings == 4  # no duplicate rows after replay


# ── 5. Path safety ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", [
    "relative/path", "/", "/srv/repos/alpha/", "/srv//repos/alpha",
    "/srv/repos/./alpha", "/srv/repos/../secret", "/srv/repos/alpha\x00suffix",
])
def test_invalid_paths_skipped_zero_store_calls(tmp_path, bad):
    am = _make_agent_mail_db(tmp_path, [{"id": 1, "slug": "a", "human_key": bad}])
    roots = pli.LegacyRoots(agent_mail_db=am)
    spy_calls: list[Any] = []

    class S:
        def import_legacy_project(self, **kw):
            spy_calls.append(kw)
            return type("R", (), {"replayed": False})()

    pli.import_legacy(S(), roots)  # type: ignore[arg-type]
    assert spy_calls == []


def test_boundary_excludes_out_of_boundary_path(tmp_path):
    am = _make_agent_mail_db(
        tmp_path, [{"id": 1, "slug": "a", "human_key": "/other/alpha"}]
    )
    roots = pli.LegacyRoots(agent_mail_db=am, local_boundary=Path("/srv"))
    spy_calls: list[Any] = []

    class S:
        def import_legacy_project(self, **kw):
            spy_calls.append(kw)
            return type("R", (), {"replayed": False})()

    pli.import_legacy(S(), roots)  # type: ignore[arg-type]
    assert spy_calls == []  # /other/alpha outside /srv boundary


# ── 6. Read-only authority immutability ────────────────────────────────────
def _signature(p: Path) -> str:
    import hashlib as h
    return h.sha256(p.read_bytes()).hexdigest() if p.is_file() else ""


def test_legacy_authorities_byte_for_byte_unchanged(tmp_path):
    roots = _full_roots(tmp_path)
    before = {
        "am": _signature(roots.agent_mail_db),
        "mp": _signature(roots.mail_projects_json),
        "cr": _signature(roots.coordination_db),
        "hr": _signature(sorted(roots.herdr_sessions_dir.glob("*/session.json"))[0]),
    }
    pli.import_legacy(_registry(tmp_path), roots)
    after = {
        "am": _signature(roots.agent_mail_db),
        "mp": _signature(roots.mail_projects_json),
        "cr": _signature(roots.coordination_db),
        "hr": _signature(sorted(roots.herdr_sessions_dir.glob("*/session.json"))[0]),
    }
    assert before == after


def test_import_never_creates_workspace_or_runtime_state(tmp_path):
    store = _registry(tmp_path)
    pli.import_legacy(store, _full_roots(tmp_path))
    con = sqlite3.connect(f"file:{store.path}?mode=ro", uri=True)
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    con.close()
    # importer only caused project/repo_location/legacy_project_bindings; no workspace/run tables exist in registry schema
    assert "workspaces" in tables  # schema exists (DDL) but...
    con = sqlite3.connect(f"file:{store.path}?mode=ro", uri=True)
    ws = con.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
    con.close()
    assert ws == 0
