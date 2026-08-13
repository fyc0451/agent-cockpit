from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent_cockpit import project_workbench_adapter as adapter
import server


PROJECT_ID = "prj_" + "a" * 32
CONFLICT_DETAIL = "项目兼容绑定冲突"


def _source_key(value):
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _binding(kind, key):
    return SimpleNamespace(source_kind=kind, source_key=key)


def _agent_mail_binding(project_id=7):
    return _binding("agent_mail_project", _source_key({"project_id": project_id}))


def _session_binding(session, session_dir, kind="herdr_session"):
    return _binding(
        kind,
        _source_key({"session": session, "session_dir": session_dir}),
    )


class Registry:
    def __init__(self, bindings, *, error=None):
        self.bindings = tuple(bindings)
        self.error = error
        self.calls = []
        self.write_calls = []

    def get_project_by_slug(self, slug):
        self.calls.append(("get_project_by_slug", slug))
        if self.error:
            raise self.error
        return SimpleNamespace(project_id=PROJECT_ID, slug=slug)

    def list_legacy_bindings(self, project_id):
        self.calls.append(("list_legacy_bindings", project_id))
        if self.error:
            raise self.error
        return self.bindings

    def create_project(self, **_kwargs):
        self.write_calls.append("create_project")

    def bind_legacy_source(self, **_kwargs):
        self.write_calls.append("bind_legacy_source")

    def import_legacy_project(self, **_kwargs):
        self.write_calls.append("import_legacy_project")


def _legacy_project(project_dir):
    return {
        "id": 7, "slug": "demo", "human_key": str(project_dir),
        "created_at": 123.0, "archived_at": None,
    }


def _setup(
    monkeypatch, tmp_path, *, bindings, sessions=(), live_project=True,
    registry_error=None,
):
    project_dir = tmp_path / "legacy-project"
    project_dir.mkdir()
    registry = Registry(bindings, error=registry_error)
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server, "_agent_mail_status", lambda: {"available": True, "reason": None},
    )
    monkeypatch.setattr(server.db, "project_by_slug", lambda slug: _legacy_project(project_dir) if slug == "demo" else None)
    monkeypatch.setattr(server, "_project_workbench_registry", lambda: registry)
    monkeypatch.setattr(server.coordination, "list_assignments", lambda _key: [])
    monkeypatch.setattr(
        server, "_herdr_runtime_snapshot",
        lambda: {"available": True, "degraded": False, "sessions": list(sessions)},
    )
    live_calls = []

    def live_binding(session, session_dir):
        live_calls.append((session, session_dir))
        return str(project_dir) if live_project else None

    monkeypatch.setattr(server.mail_projects, "get", live_binding)
    return TestClient(server.app), registry, live_calls


def _get(client, slug="demo"):
    return client.get(
        f"/api/projects/{slug}/workbench",
        headers={"authorization": "Bearer secret"},
    )


def test_source_key_vectors_match_frozen_import_rule():
    assert adapter.legacy_source_key({"project_id": 7}) == (
        "sha256:61ff7589883d8a29cf7bcc287f3a26aeab9f935f949f9095a4fd4536c04ba9b9"
    )
    assert adapter.legacy_source_key({
        "session": "target", "session_dir": "/sessions/target",
    }) == "sha256:4476e9ad2719635728ce475449724792a149f607d5c29580146b3510bc03acdd"


@pytest.mark.parametrize("kind", ["mail_projects_session", "herdr_session"])
def test_exact_imported_session_provenance_accepts_only_frozen_kinds(
    monkeypatch, tmp_path, kind,
):
    session = {
        "session": "target", "directory": "/sessions/target",
        "status": "running", "panes": [],
    }
    client, _registry, _calls = _setup(
        monkeypatch, tmp_path,
        bindings=[_agent_mail_binding(), _session_binding("target", "/sessions/target", kind)],
        sessions=(session,),
    )

    response = _get(client)

    assert response.status_code == 200
    assert [item["session"] for item in response.json()["sessions"]] == ["target"]


def test_unaccepted_exact_source_kind_does_not_prove_session(monkeypatch, tmp_path):
    session = {
        "session": "target", "directory": "/sessions/target",
        "status": "running", "panes": [],
    }
    client, _registry, _calls = _setup(
        monkeypatch, tmp_path,
        bindings=[
            _agent_mail_binding(),
            _binding(
                "coordination_run",
                _source_key({"session": "target", "session_dir": "/sessions/target"}),
            ),
        ],
        sessions=(session,),
    )

    response = _get(client)

    assert response.status_code == 409
    assert response.json() == {"detail": CONFLICT_DETAIL}


@pytest.mark.parametrize("bindings", [[], [_agent_mail_binding(8)]])
def test_agent_mail_project_provenance_missing_or_wrong_is_409(
    monkeypatch, tmp_path, bindings,
):
    client, _registry, _calls = _setup(
        monkeypatch, tmp_path, bindings=bindings,
    )

    response = _get(client)

    assert response.status_code == 409
    assert response.json() == {"detail": CONFLICT_DETAIL}


@pytest.mark.parametrize(
    "bindings",
    [
        [_agent_mail_binding()],
        [_agent_mail_binding(), _session_binding("target", "/sessions/old")],
    ],
)
def test_exact_session_provenance_missing_or_wrong_is_409(
    monkeypatch, tmp_path, bindings,
):
    session = {
        "session": "target", "directory": "/sessions/current",
        "status": "running", "panes": [],
    }
    client, _registry, _calls = _setup(
        monkeypatch, tmp_path, bindings=bindings, sessions=(session,),
    )

    response = _get(client)

    assert response.status_code == 409
    assert response.json() == {"detail": CONFLICT_DETAIL}


def test_same_name_session_or_slug_never_proves_ownership(monkeypatch, tmp_path):
    session = {
        "session": "demo", "directory": "/sessions/current",
        "status": "running", "panes": [],
    }
    bindings = [
        _agent_mail_binding(), _session_binding("demo", "/sessions/current"),
    ]
    client, registry, live_calls = _setup(
        monkeypatch, tmp_path, bindings=bindings, sessions=(session,),
        live_project=False,
    )

    response = _get(client)

    assert response.status_code == 200
    assert response.json()["sessions"] == []
    assert live_calls == [("demo", "/sessions/current")]
    assert registry.calls == [
        ("get_project_by_slug", "demo"),
        ("list_legacy_bindings", PROJECT_ID),
    ]


def test_success_remains_strict_four_key_legacy_body(monkeypatch, tmp_path):
    session = {
        "session": "target", "directory": "/sessions/target",
        "status": "running", "focused_pane_id": None, "panes": [],
    }
    client, registry, _calls = _setup(
        monkeypatch, tmp_path,
        bindings=[_agent_mail_binding(), _session_binding("target", "/sessions/target")],
        sessions=(session,),
    )

    response = _get(client)

    assert response.status_code == 200
    assert set(response.json()) == {"project", "assignments", "sessions", "source"}
    assert response.json()["project"] == {"id": 7, "slug": "demo", "created_at": 123.0}
    assert response.json()["sessions"] == [{
        "session": "target", "status": "running",
        "focused_pane_id": None, "panes": [],
    }]
    assert PROJECT_ID not in response.text
    assert registry.write_calls == []


def test_registry_failure_is_stable_redacted_409(monkeypatch, tmp_path):
    raw = "private/path project_id=secret SELECT source_key"
    client, _registry, _calls = _setup(
        monkeypatch, tmp_path, bindings=[], registry_error=OSError(raw),
    )

    response = _get(client)

    assert response.status_code == 409
    assert response.json() == {"detail": CONFLICT_DETAIL}
    assert raw not in response.text
    for fragment in ("private/path", "SELECT", "source_key", "secret"):
        assert fragment not in response.text


def test_cold_server_provider_opens_existing_without_creating_store(
    monkeypatch, tmp_path,
):
    store_path = tmp_path / "project-registry.sqlite3"
    calls = []
    real_open_existing = server.project_registry_store.open_existing

    def fail_initialize(*_args, **_kwargs):
        raise AssertionError("initialize must not be called")

    def tracked_open_existing(path):
        calls.append(path)
        return real_open_existing(path)

    monkeypatch.setattr(server, "_project_registry_store", None)
    monkeypatch.setattr(
        server.runtime_paths, "store",
        lambda name: store_path if name == "project_registry" else None,
    )
    monkeypatch.setattr(
        server.project_registry_store, "initialize", fail_initialize,
    )
    monkeypatch.setattr(
        server.project_registry_store, "open_existing", tracked_open_existing,
    )
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server, "_agent_mail_status", lambda: {"available": True, "reason": None},
    )
    project_dir = tmp_path / "legacy-project"
    project_dir.mkdir()
    monkeypatch.setattr(
        server.db, "project_by_slug", lambda _slug: _legacy_project(project_dir),
    )

    response = _get(TestClient(server.app))

    assert response.status_code == 409
    assert response.json() == {"detail": CONFLICT_DETAIL}
    assert calls == [store_path]
    assert not store_path.exists()
    assert not (tmp_path / "project-registry.sqlite3-wal").exists()
    assert not (tmp_path / "project-registry.sqlite3-shm").exists()


def test_cached_server_provider_is_reused_without_open_or_initialize(monkeypatch):
    cached = object()
    monkeypatch.setattr(server, "_project_registry_store", cached)
    monkeypatch.setattr(
        server.project_registry_store, "initialize",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("initialize")),
    )
    monkeypatch.setattr(
        server.project_registry_store, "open_existing",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("open_existing")),
    )

    assert server._project_workbench_registry() is cached


def test_cold_server_provider_reads_existing_store_without_mutation(
    monkeypatch, tmp_path,
):
    store_path = tmp_path / "project-registry.sqlite3"
    registry = server.project_registry_store.initialize(store_path)
    project = registry.create_project(slug="demo", display_name="Demo", goal=None)
    registry.bind_legacy_source(
        project_id=project.project_id,
        source_kind="agent_mail_project",
        source_key=_source_key({"project_id": 7}),
        source_digest=_source_key({"legacy_project": 7}),
    )
    registry.close()
    before_bytes = store_path.read_bytes()
    before_stat = store_path.stat()
    before_metadata = (
        before_stat.st_dev, before_stat.st_ino, before_stat.st_mode,
        before_stat.st_nlink, before_stat.st_size, before_stat.st_mtime_ns,
        before_stat.st_ctime_ns,
    )
    before_sidecars = {
        path.name for path in tmp_path.iterdir()
        if path.name.startswith(store_path.name + "-")
    }
    open_calls = []
    real_open_existing = server.project_registry_store.open_existing

    def fail_initialize(*_args, **_kwargs):
        raise AssertionError("initialize must not be called")

    def tracked_open_existing(path):
        open_calls.append(path)
        return real_open_existing(path)

    monkeypatch.setattr(server, "_project_registry_store", None)
    monkeypatch.setattr(
        server.runtime_paths, "store",
        lambda name: store_path if name == "project_registry" else None,
    )
    monkeypatch.setattr(
        server.project_registry_store, "initialize", fail_initialize,
    )
    monkeypatch.setattr(
        server.project_registry_store, "open_existing", tracked_open_existing,
    )
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server, "_agent_mail_status", lambda: {"available": True, "reason": None},
    )
    project_dir = tmp_path / "legacy-project"
    project_dir.mkdir()
    monkeypatch.setattr(
        server.db, "project_by_slug", lambda _slug: _legacy_project(project_dir),
    )
    monkeypatch.setattr(server.coordination, "list_assignments", lambda _key: [])
    monkeypatch.setattr(
        server, "_herdr_runtime_snapshot",
        lambda: {"available": False, "degraded": True},
    )

    response = _get(TestClient(server.app))

    assert response.status_code == 200
    assert set(response.json()) == {"project", "assignments", "sessions", "source"}
    assert response.json()["sessions"] == []
    assert open_calls == [store_path]
    assert server._project_registry_store is None
    after_stat = store_path.stat()
    assert store_path.read_bytes() == before_bytes
    assert (
        after_stat.st_dev, after_stat.st_ino, after_stat.st_mode,
        after_stat.st_nlink, after_stat.st_size, after_stat.st_mtime_ns,
        after_stat.st_ctime_ns,
    ) == before_metadata
    assert {
        path.name for path in tmp_path.iterdir()
        if path.name.startswith(store_path.name + "-")
    } == before_sidecars


def test_unexpected_live_binding_failure_is_stable_redacted_409(
    monkeypatch, tmp_path,
):
    raw = "private/path SELECT source_key secret"
    session = {
        "session": "target", "directory": "/sessions/target",
        "status": "running", "panes": [],
    }
    client, _registry, _calls = _setup(
        monkeypatch, tmp_path,
        bindings=[_agent_mail_binding(), _session_binding("target", "/sessions/target")],
        sessions=(session,),
    )
    monkeypatch.setattr(
        server.mail_projects, "get",
        lambda *_args: (_ for _ in ()).throw(RuntimeError(raw)),
    )

    response = _get(client)

    assert response.status_code == 409
    assert response.json() == {"detail": CONFLICT_DETAIL}
    for fragment in ("private/path", "SELECT", "source_key", "secret"):
        assert fragment not in response.text


def test_duplicate_exact_live_session_is_stable_409(monkeypatch, tmp_path):
    session = {
        "session": "target", "directory": "/sessions/target",
        "status": "running", "panes": [],
    }
    client, _registry, _calls = _setup(
        monkeypatch, tmp_path,
        bindings=[_agent_mail_binding(), _session_binding("target", "/sessions/target")],
        sessions=(session, dict(session)),
    )

    response = _get(client)

    assert response.status_code == 409
    assert response.json() == {"detail": CONFLICT_DETAIL}


@pytest.mark.parametrize("sessions", [None, "target", {"session": "target"}, 7])
def test_malformed_sessions_container_is_200_degraded_empty(
    monkeypatch, tmp_path, sessions,
):
    client, _registry, _calls = _setup(
        monkeypatch, tmp_path, bindings=[_agent_mail_binding()],
    )
    monkeypatch.setattr(
        server, "_herdr_runtime_snapshot",
        lambda: {"available": True, "degraded": False, "sessions": sessions},
    )

    response = _get(client)

    assert response.status_code == 200
    assert response.json()["sessions"] == []
    assert response.json()["source"]["available"] is True
    assert response.json()["source"]["degraded"] is True


def test_malformed_mail_status_is_fixed_503(monkeypatch, tmp_path):
    client, _registry, _calls = _setup(
        monkeypatch, tmp_path, bindings=[_agent_mail_binding()],
    )
    monkeypatch.setattr(server, "_agent_mail_status", lambda: "private/path")

    response = _get(client)

    assert response.status_code == 503
    assert response.json() == {"detail": "Agent Mail 查询失败"}
    assert "private/path" not in response.text


@pytest.mark.parametrize("rows", [7, "private/path", [None]])
def test_malformed_assignment_rows_are_fixed_503(
    monkeypatch, tmp_path, rows,
):
    client, _registry, _calls = _setup(
        monkeypatch, tmp_path, bindings=[_agent_mail_binding()],
    )
    monkeypatch.setattr(server.coordination, "list_assignments", lambda _key: rows)

    response = _get(client)

    assert response.status_code == 503
    assert response.json() == {"detail": "Coordination 查询失败"}
    assert "private/path" not in response.text


@pytest.mark.parametrize(
    "project",
    [
        {"slug": "demo", "human_key": "/legacy"},
        {"id": 7, "human_key": "/legacy"},
        {"id": 7, "slug": "demo"},
        {"id": 7, "slug": "other", "human_key": "/legacy"},
    ],
)
def test_incomplete_legacy_project_is_fixed_409(
    monkeypatch, tmp_path, project,
):
    client, _registry, _calls = _setup(
        monkeypatch, tmp_path, bindings=[_agent_mail_binding()],
    )
    monkeypatch.setattr(server.db, "project_by_slug", lambda _slug: project)

    response = _get(client)

    assert response.status_code == 409
    assert response.json() == {"detail": CONFLICT_DETAIL}


@pytest.mark.parametrize(
    "record",
    [SimpleNamespace(slug="demo"), SimpleNamespace(project_id=PROJECT_ID),
     SimpleNamespace(project_id=PROJECT_ID, slug="other")],
)
def test_incomplete_registry_project_is_fixed_409(
    monkeypatch, tmp_path, record,
):
    client, registry, _calls = _setup(
        monkeypatch, tmp_path, bindings=[_agent_mail_binding()],
    )
    registry.get_project_by_slug = lambda _slug: record

    response = _get(client)

    assert response.status_code == 409
    assert response.json() == {"detail": CONFLICT_DETAIL}


def test_adapter_calls_read_providers_only(monkeypatch, tmp_path):
    session = {
        "session": "target", "directory": "/sessions/target",
        "status": "running", "panes": [],
    }
    client, registry, _calls = _setup(
        monkeypatch, tmp_path,
        bindings=[_agent_mail_binding(), _session_binding("target", "/sessions/target")],
        sessions=(session,),
    )
    forbidden_calls = []

    def forbidden(name):
        def call(*_args, **_kwargs):
            forbidden_calls.append(name)
            raise AssertionError(name)
        return call

    for name in ("bind", "unbind"):
        monkeypatch.setattr(server.mail_projects, name, forbidden(f"mail_projects.{name}"))
    for name in ("create_assignment", "close_assignment", "start_run"):
        monkeypatch.setattr(server.coordination, name, forbidden(f"coordination.{name}"))
    for name in ("start_agent", "stop_session"):
        monkeypatch.setattr(server.herdr_client, name, forbidden(f"herdr_client.{name}"))

    assert _get(client).status_code == 200
    assert registry.write_calls == []
    assert forbidden_calls == []


def test_registry_project_id_is_not_accepted_as_legacy_slug(monkeypatch, tmp_path):
    client, registry, _calls = _setup(
        monkeypatch, tmp_path, bindings=[_agent_mail_binding()],
    )

    response = _get(client, PROJECT_ID)

    assert response.status_code == 404
    assert registry.calls == []
