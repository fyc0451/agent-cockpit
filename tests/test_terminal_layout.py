"""终端布局:分屏/拆开/组合 — /api/herdr layout 端点。"""
from fastapi.testclient import TestClient

import server

AUTH = {"authorization": "Bearer secret"}


def _client(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(server.herdr_client, "is_available", lambda: True)
    return TestClient(server.app)


def test_pane_layout_split_validates_and_dispatches(monkeypatch):
    captured = {}

    def fake_split(session, pane_id, mode):
        captured.update(session=session, pane_id=pane_id, mode=mode)
        return ["w1:p9"]

    monkeypatch.setattr(server.herdr_client, "split_pane_layout", fake_split)
    client = _client(monkeypatch)

    resp = client.post(
        "/api/herdr/pane/demo/w1:p1/layout/split",
        json={"mode": "grid4"}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"mode": "grid4", "created": ["w1:p9"]}
    assert captured == {"session": "demo", "pane_id": "w1:p1", "mode": "grid4"}

    assert client.post(
        "/api/herdr/pane/demo/w1:p1/layout/split",
        json={"mode": "diagonal"}, headers=AUTH).status_code == 400
    assert client.post(
        "/api/herdr/pane/bad..name/w1:p1/layout/split",
        json={"mode": "horizontal"}, headers=AUTH).status_code == 400


def test_pane_layout_detach_dispatches(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server.herdr_client, "detach_pane",
        lambda session, pane_id: calls.append((session, pane_id)))
    client = _client(monkeypatch)

    resp = client.post(
        "/api/herdr/pane/demo/w1:p3/layout/detach", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"detached": "w1:p3"}
    assert calls == [("demo", "w1:p3")]

    monkeypatch.setattr(
        server.herdr_client, "detach_pane",
        lambda session, pane_id: (_ for _ in ()).throw(RuntimeError("herdr 挂了")))
    assert client.post(
        "/api/herdr/pane/demo/w1:p3/layout/detach",
        headers=AUTH).status_code == 502


def test_session_layout_untile_validates_tab_id(monkeypatch):
    captured = []
    monkeypatch.setattr(
        server.herdr_client, "untile_tab",
        lambda session, tab_id: captured.append((session, tab_id)) or ["w1:p2"])
    client = _client(monkeypatch)

    resp = client.post(
        "/api/herdr/session/demo/layout/untile",
        json={"tab_id": "w1:t2"}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"tab_id": "w1:t2", "moved": ["w1:p2"]}
    assert captured == [("demo", "w1:t2")]

    assert client.post(
        "/api/herdr/session/demo/layout/untile",
        json={"tab_id": "bad tab"}, headers=AUTH).status_code == 400
    assert client.post(
        "/api/herdr/session/bad..name/layout/untile",
        json={"tab_id": "w1:t2"}, headers=AUTH).status_code == 400


def test_session_layout_compose_validates_and_dispatches(monkeypatch):
    captured = {}

    def fake_compose(session, pane_ids, orientation):
        captured.update(session=session, pane_ids=list(pane_ids), orientation=orientation)
        return pane_ids[0]

    monkeypatch.setattr(server.herdr_client, "compose_panes", fake_compose)
    client = _client(monkeypatch)

    resp = client.post(
        "/api/herdr/session/demo/layout/compose",
        json={"pane_ids": ["w1:p1", "w1:p2"], "orientation": "vertical"},
        headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"base": "w1:p1", "composed": 2}
    assert captured == {
        "session": "demo", "pane_ids": ["w1:p1", "w1:p2"],
        "orientation": "vertical",
    }

    # 1 个 / 5 个 pane、方向非法、pane id 非法 → 400
    for body in (
        {"pane_ids": ["w1:p1"], "orientation": "horizontal"},
        {"pane_ids": ["w1:p%d" % i for i in range(6)], "orientation": "horizontal"},
        {"pane_ids": ["w1:p1", "w1:p2"], "orientation": "diagonal"},
        {"pane_ids": ["w1:p1", "bad pane"], "orientation": "horizontal"},
    ):
        assert client.post(
            "/api/herdr/session/demo/layout/compose",
            json=body, headers=AUTH).status_code == 400

    # herdr_client 的语义错误(pane 不存在)映射 400,运行错误映射 502
    monkeypatch.setattr(
        server.herdr_client, "compose_panes",
        lambda *a: (_ for _ in ()).throw(ValueError("未找到 pane")))
    assert client.post(
        "/api/herdr/session/demo/layout/compose",
        json={"pane_ids": ["w1:p1", "w1:p2"]}, headers=AUTH).status_code == 400
    monkeypatch.setattr(
        server.herdr_client, "compose_panes",
        lambda *a: (_ for _ in ()).throw(RuntimeError("herdr 挂了")))
    assert client.post(
        "/api/herdr/session/demo/layout/compose",
        json={"pane_ids": ["w1:p1", "w1:p2"]}, headers=AUTH).status_code == 502


def test_layout_frontend_entry_and_modal_present():
    html = (server.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "showLayoutModal()" in html
    assert 'id="layoutModal"' in html
    assert "layoutSplit('grid4')" in html
    assert "layoutDetach()" in html
    assert "layoutUntile()" in html
    assert "layoutCompose()" in html
    assert "/layout/compose" in html
    assert "term.layout" in html
