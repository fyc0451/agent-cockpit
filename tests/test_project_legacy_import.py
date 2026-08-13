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
import os
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
        "CREATE TABLE projects (id INTEGER PRIMARY KEY, slug TEXT, human_key TEXT, "
        "created_at TEXT, archived_at TEXT)"
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


def _make_herdr_session(tmp_path: Path, session: str, session_dir: str,
                        workspaces: list[dict[str, str]], version: int = 3) -> Path:
    directory = tmp_path / "herdr-sessions"
    directory.mkdir(exist_ok=True)
    fname = f"{session}.json"
    (directory / fname).write_text(
        json.dumps(
            {"session": session, "session_dir": session_dir, "version": version, "workspaces": workspaces}
        ),
        encoding="utf-8",
    )
    return directory


def _make_coordination_db(tmp_path: Path, runs: list[dict[str, Any]]) -> Path:
    db = tmp_path / "coordination.sqlite3"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, project_key TEXT, session TEXT, "
        "session_dir TEXT, revision INTEGER, state TEXT, config_hash TEXT, "
        "started_ts REAL, closed_ts REAL)"
    )
    for r in runs:
        con.execute(
            "INSERT INTO runs (run_id, project_key, session, session_dir, revision, state) "
            "VALUES (?,?,?,?,?,?)",
            (r["run_id"], r["project_key"], r["session"], r["session_dir"], r["revision"], "running"),
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
    mp = _make_mail_projects(
        tmp_path,
        {"alpha-dev": {"session_dir": HERDR_SD, "project": ALPHA}},
    )
    hr = _make_herdr_session(
        tmp_path, "alpha-dev", HERDR_SD,
        [{"workspace_id": "w1", "identity_cwd": ALPHA}],
    )
    cr = _make_coordination_db(
        tmp_path,
        [{"run_id": "run_01JABCDEF0123456789ABCDEF", "project_key": ALPHA,
          "session": "alpha-dev", "session_dir": HERDR_SD, "revision": 1}],
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
        "hr": _signature(sorted(roots.herdr_sessions_dir.glob("*.json"))[0]),
    }
    pli.import_legacy(_registry(tmp_path), roots)
    after = {
        "am": _signature(roots.agent_mail_db),
        "mp": _signature(roots.mail_projects_json),
        "cr": _signature(roots.coordination_db),
        "hr": _signature(sorted(roots.herdr_sessions_dir.glob("*.json"))[0]),
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
