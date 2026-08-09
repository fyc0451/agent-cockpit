import pytest

import server


@pytest.fixture(autouse=True)
def reset_zoom_leases():
    server._ZOOM_LEASES.clear()
    yield
    server._ZOOM_LEASES.clear()


def _layout(*, zoomed=False, horizontal=True, pane_id="w1:p2", pane_count=2):
    return {
        "available": True,
        "zoomed": zoomed,
        "horizontal_split": horizontal,
        "focused_pane_id": pane_id,
        "tab_id": "w1:t1",
        "panes": [{"pane_id": f"w1:p{index + 1}"} for index in range(pane_count)],
    }


def _term_list(*ids):
    return [{"id": term_id} for term_id in ids]


def test_zoom_lease_acquire_renew_and_release_are_owned_and_idempotent(monkeypatch):
    monkeypatch.setattr(server.terminal, "list_terms", lambda: _term_list("term1"))
    zoomed = {"value": False}
    calls = []

    def pane_layout(session, pane_id=None):
        return _layout(zoomed=zoomed["value"], pane_id=pane_id or "w1:p2")

    def pane_zoom(session, pane_id, mode):
        calls.append((session, pane_id, mode))
        zoomed["value"] = mode == "on"
        return {
            "available": True, "zoomed": zoomed["value"], "changed": True,
            "focused_pane_id": pane_id,
        }

    monkeypatch.setattr(server.herdr_client, "pane_layout", pane_layout)
    monkeypatch.setattr(server.herdr_client, "pane_zoom", pane_zoom)

    acquired = server._acquire_zoom_lease("demo", "term1", now=100)
    renewed = server._renew_zoom_lease("demo", "term1", now=110)
    released = server._release_zoom_lease("demo", "term1", now=120)
    released_again = server._release_zoom_lease("demo", "term1", now=121)

    assert acquired["acquired"] is True
    assert renewed["renewed"] is True
    assert renewed["owned"] is True
    assert released == {
        "available": True, "released": True, "owned": False,
        "changed": True, "session": "demo",
    }
    assert released_again["released"] is True
    assert released_again["changed"] is False
    assert calls == [("demo", "w1:p2", "on"), ("demo", "w1:p2", "off")]
    assert "demo" not in server._ZOOM_LEASES


def test_zoom_lease_rejects_existing_manual_zoom_without_unzooming(monkeypatch):
    monkeypatch.setattr(server.terminal, "list_terms", lambda: _term_list("term1"))
    monkeypatch.setattr(
        server.herdr_client, "pane_layout", lambda *args: _layout(zoomed=True)
    )
    monkeypatch.setattr(
        server.herdr_client, "pane_zoom",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不得抢占手动 zoom")),
    )

    result = server._acquire_zoom_lease("demo", "term1", now=100)

    assert result["acquired"] is False
    assert result["reason"] == "already_zoomed"
    assert server._release_zoom_lease("demo", "term1")["changed"] is False


def test_zoom_lease_does_not_own_manual_zoom_that_wins_after_layout_check(monkeypatch):
    monkeypatch.setattr(server.terminal, "list_terms", lambda: _term_list("term1"))
    monkeypatch.setattr(
        server.herdr_client, "pane_layout", lambda *args: _layout(zoomed=False)
    )
    calls = []

    def raced_zoom(session, pane_id, mode):
        calls.append(mode)
        return {
            "available": True, "zoomed": True, "changed": False,
            "reason": "already_zoomed", "focused_pane_id": pane_id,
        }

    monkeypatch.setattr(server.herdr_client, "pane_zoom", raced_zoom)

    result = server._acquire_zoom_lease("demo", "term1", now=100)

    assert result["acquired"] is False
    assert result["reason"] == "already_zoomed"
    assert server._ZOOM_LEASES == {}
    assert server._release_zoom_lease("demo", "term1")["changed"] is False
    assert calls == ["on"]


def test_zoom_lease_applies_to_vertical_multi_pane(monkeypatch):
    monkeypatch.setattr(server.terminal, "list_terms", lambda: _term_list("term1"))
    monkeypatch.setattr(
        server.herdr_client, "pane_layout",
        lambda *args: _layout(zoomed=False, horizontal=False),
    )
    calls = []
    monkeypatch.setattr(
        server.herdr_client, "pane_zoom",
        lambda session, pane_id, mode: calls.append((session, pane_id, mode)) or {
            "available": True, "zoomed": True, "changed": True,
            "focused_pane_id": pane_id,
        },
    )

    result = server._acquire_zoom_lease("demo", "term1", now=100)

    assert result["acquired"] is True
    assert result["owned"] is True
    assert calls == [("demo", "w1:p2", "on")]


def test_zoom_lease_skips_single_pane_tab(monkeypatch):
    monkeypatch.setattr(server.terminal, "list_terms", lambda: _term_list("term1"))
    monkeypatch.setattr(
        server.herdr_client, "pane_layout",
        lambda *args: _layout(zoomed=False, horizontal=False, pane_count=1),
    )
    monkeypatch.setattr(
        server.herdr_client, "pane_zoom",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("单 pane 不应启动 zoom")
        ),
    )

    result = server._acquire_zoom_lease("demo", "term1", now=100)

    assert result["acquired"] is False
    assert result["reason"] == "single_pane"


def test_zoom_lease_does_not_cross_terminal_owners(monkeypatch):
    monkeypatch.setattr(
        server.terminal, "list_terms", lambda: _term_list("term1", "term2")
    )
    monkeypatch.setattr(
        server.herdr_client, "pane_layout", lambda *args: _layout(zoomed=False)
    )
    calls = []

    def pane_zoom(session, pane_id, mode):
        calls.append(mode)
        return {"available": True, "zoomed": mode == "on", "changed": True}

    monkeypatch.setattr(server.herdr_client, "pane_zoom", pane_zoom)

    assert server._acquire_zoom_lease("demo", "term1", now=100)["acquired"] is True
    blocked = server._acquire_zoom_lease("demo", "term2", now=101)
    wrong_release = server._release_zoom_lease("demo", "term2", now=102)

    assert blocked["reason"] == "leased"
    assert wrong_release["reason"] == "not_owner"
    assert server._ZOOM_LEASES["demo"]["owner"] == "term1"
    assert calls == ["on"]


def test_expired_zoom_lease_unzooms_and_failed_release_retries(monkeypatch):
    server._ZOOM_LEASES["demo"] = {
        "owner": "term1", "pane_id": "w1:p2", "tab_id": "w1:t1",
        "expires_at": 30,
    }
    responses = iter([
        {"available": True, "error": "socket down"},
        {"available": True, "zoomed": False, "changed": True},
    ])
    monkeypatch.setattr(server.herdr_client, "pane_zoom", lambda *args, **kwargs: next(responses))

    first = server._expire_zoom_leases(now=31)
    assert first[0]["released"] is False
    assert server._ZOOM_LEASES["demo"]["expires_at"] == 31 + server.ZOOM_LEASE_RETRY

    second = server._expire_zoom_leases(now=31 + server.ZOOM_LEASE_RETRY)
    assert second[0]["released"] is True
    assert "demo" not in server._ZOOM_LEASES


def test_terminal_delete_releases_its_zoom_lease_before_kill(monkeypatch):
    server._ZOOM_LEASES["demo"] = {
        "owner": "term1", "pane_id": "w1:p2", "tab_id": "w1:t1",
        "expires_at": 100,
    }
    events = []
    monkeypatch.setattr(
        server.herdr_client, "pane_zoom",
        lambda *args, **kwargs: events.append("unzoom") or {
            "available": True, "zoomed": False, "changed": True,
        },
    )
    monkeypatch.setattr(
        server.terminal, "kill_term", lambda term_id: events.append(f"kill:{term_id}")
    )

    assert server.api_term_kill("term1") == {"ok": True}
    assert events == ["unzoom", "kill:term1"]
