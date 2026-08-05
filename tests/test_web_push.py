import json

from fastapi.testclient import TestClient

import server
import web_push


def _subscription(endpoint="https://push.example/subscription"):
    return {
        "endpoint": endpoint,
        "expirationTime": None,
        "keys": {"p256dh": "cHVibGljLWtleQ", "auth": "YXV0aC1rZXk"},
    }


def test_subscription_round_trip_uses_private_state_db(tmp_path, monkeypatch):
    monkeypatch.setattr(web_push, "DB_PATH", tmp_path / "push.sqlite3")

    saved = web_push.save_subscription(_subscription())
    assert saved == {"ok": True}
    assert web_push.list_subscriptions() == [_subscription()]

    assert web_push.delete_subscription("https://push.example/subscription") is True
    assert web_push.list_subscriptions() == []


def test_subscription_rejects_non_https_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(web_push, "DB_PATH", tmp_path / "push.sqlite3")

    try:
        web_push.save_subscription(_subscription("http://push.example/subscription"))
    except ValueError as exc:
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("non-HTTPS push endpoint accepted")


def test_config_generates_private_vapid_key_once(tmp_path, monkeypatch):
    monkeypatch.delenv("COCKPIT_VAPID_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("COCKPIT_VAPID_PUBLIC_KEY", raising=False)
    key_path = tmp_path / "vapid-private.pem"
    monkeypatch.setattr(web_push, "KEY_PATH", key_path)

    first = web_push.config()
    second = web_push.config()

    assert first["available"] is True
    assert first["public_key"] == second["public_key"]
    assert len(first["public_key"]) == 87
    assert key_path.stat().st_mode & 0o777 == 0o600


def test_config_rejects_mismatched_explicit_vapid_keys(tmp_path, monkeypatch):
    key_path = tmp_path / "vapid-private.pem"
    monkeypatch.setattr(web_push, "KEY_PATH", key_path)
    private, public = web_push._ensure_private_key()
    monkeypatch.setenv("COCKPIT_VAPID_PRIVATE_KEY", private)
    monkeypatch.setenv("COCKPIT_VAPID_PUBLIC_KEY", "wrong-public-key")

    mismatch = web_push.config()

    assert mismatch["available"] is False
    assert "不匹配" in mismatch["reason"]

    monkeypatch.setenv("COCKPIT_VAPID_PUBLIC_KEY", public)
    assert web_push.config()["available"] is True


def test_notify_sends_deep_link_and_removes_gone_subscription(tmp_path, monkeypatch):
    monkeypatch.setattr(web_push, "DB_PATH", tmp_path / "push.sqlite3")
    web_push.save_subscription(_subscription())
    calls = []

    class Gone(Exception):
        response = type("Response", (), {"status_code": 410})()

    monkeypatch.setattr(
        web_push,
        "config",
        lambda: {
            "available": True,
            "public_key": "public",
            "private_key": "/private/key.pem",
            "subject": "mailto:test@example.com",
        },
    )

    def fake_send(**kwargs):
        calls.append(kwargs)
        raise Gone()

    monkeypatch.setattr(web_push, "_send", fake_send)

    result = web_push.notify(
        [
            {
                "id": "pane:demo:w1:p2",
                "title": "codex 等待你",
                "detail": "demo · project",
                "url": "/#/attention/pane/demo/w1%3Ap2",
            }
        ]
    )

    payload = json.loads(calls[0]["data"])
    assert payload["url"] == "/#/attention/pane/demo/w1%3Ap2"
    assert payload["tag"] == "pane:demo:w1:p2"
    assert result == {"sent": 0, "removed": 1, "failed": 0}
    assert web_push.list_subscriptions() == []


def test_push_routes_expose_config_and_store_subscription(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server.web_push,
        "public_config",
        lambda: {"available": True, "public_key": "vapid-public"},
    )
    saved = []
    monkeypatch.setattr(server.web_push, "save_subscription", lambda value: saved.append(value) or {"ok": True})
    client = TestClient(server.app)
    headers = {"authorization": "Bearer secret"}

    assert client.get("/api/push/config", headers=headers).json() == {
        "available": True,
        "public_key": "vapid-public",
    }
    response = client.post("/api/push/subscriptions", headers=headers, json=_subscription())
    assert response.status_code == 200
    assert saved == [_subscription()]
    worker = client.get("/sw.js")
    assert worker.status_code == 200
    assert "application/javascript" in worker.headers["content-type"]
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.json()["scope"] == "/"
    assert manifest.json()["display"] == "standalone"


def test_service_worker_and_frontend_wire_push_and_deep_links():
    html = (web_push.ROOT / "static" / "index.html").read_text()
    worker = (web_push.ROOT / "static" / "sw.js").read_text()

    assert 'data-view="attention"' in html
    assert 'id="view-attention"' in html
    assert 'rel="manifest" href="/manifest.webmanifest"' in html
    assert "/api/attention" in html
    assert "/api/push/config" in html
    assert "/api/push/subscriptions" in html
    assert "PUSH_SUBSCRIPTION.unsubscribe()" in html
    assert "Notification.requestPermission" in html
    assert "iPhone 请先在 Safari 中“添加到主屏幕”" in html
    assert "navigator.serviceWorker.register('/sw.js')" in html
    assert "location.hash" in html
    assert "#/attention/" in html
    assert "self.addEventListener('push'" in worker
    assert "Array.isArray(data)" in worker
    assert "self.addEventListener('notificationclick'" in worker
    assert "clients.openWindow" in worker
