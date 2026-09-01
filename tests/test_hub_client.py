import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "agent-mail-tools")
)

import am_common
from agent_cockpit import hub_client
from agent_cockpit import settings


def _write_client_env(monkeypatch, tmp_path, content: str) -> None:
    env_file = tmp_path / "client.env"
    env_file.write_text(content)
    monkeypatch.setattr(am_common, "CLIENT_ENV", env_file)


def test_load_config_defaults_to_localhost_without_client_env(monkeypatch, tmp_path):
    env_file = tmp_path / "client.env"
    assert not env_file.exists()
    monkeypatch.setattr(am_common, "CLIENT_ENV", env_file)

    assert am_common.load_client_config() == ("http://127.0.0.1:8765", "")


def test_load_config_uses_client_env_hub_for_shared_team_hub(monkeypatch, tmp_path):
    _write_client_env(
        monkeypatch,
        tmp_path,
        "hub=http://team-server:8765\ntoken=secret123\n",
    )

    assert am_common.load_client_config() == ("http://team-server:8765", "secret123")


def test_load_config_keeps_localhost_when_hub_unset(monkeypatch, tmp_path):
    _write_client_env(monkeypatch, tmp_path, "token=abc\n")

    assert am_common.load_client_config() == ("http://127.0.0.1:8765", "abc")


def test_load_config_ignores_comments_and_empty_hub(monkeypatch, tmp_path):
    _write_client_env(
        monkeypatch,
        tmp_path,
        "# hub=http://ignored.example:8765\nhub=\ntoken=xyz\n",
    )

    assert am_common.load_client_config() == ("http://127.0.0.1:8765", "xyz")


def test_save_client_hub_preserves_token_and_unknown_lines(monkeypatch, tmp_path):
    _write_client_env(
        monkeypatch,
        tmp_path,
        "# local config\nhub=http://old-host:8765\ntoken=secret123\nfuture=value\n",
    )

    assert am_common.save_client_hub("https://team.example:9765/") == (
        "https://team.example:9765"
    )

    env_file = tmp_path / "client.env"
    assert env_file.read_text() == (
        "# local config\nhub=https://team.example:9765\n"
        "token=secret123\nfuture=value\n"
    )
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_save_client_hub_creates_parent_and_appends_hub(monkeypatch, tmp_path):
    env_file = tmp_path / "nested" / "client.env"
    monkeypatch.setattr(am_common, "CLIENT_ENV", env_file)

    am_common.save_client_hub("http://10.0.0.8:8765")

    assert env_file.read_text() == "hub=http://10.0.0.8:8765\n"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "hub",
    [
        "",
        "team.example:8765",
        "ftp://team.example:8765",
        "http://",
        "http://user:pass@team.example:8765",
        "http://team.example:8765?token=secret",
        "http://team.example:8765/#fragment",
        "http://team.example:99999",
    ],
)
def test_save_client_hub_rejects_unsafe_or_invalid_urls(monkeypatch, tmp_path, hub):
    env_file = tmp_path / "client.env"
    env_file.write_text("token=keep-me\n")
    monkeypatch.setattr(am_common, "CLIENT_ENV", env_file)

    with pytest.raises(ValueError):
        am_common.save_client_hub(hub)

    assert env_file.read_text() == "token=keep-me\n"


def test_hub_client_reuses_am_common_parser():
    assert hub_client.load_client_config is am_common.load_client_config


def test_reload_config_updates_live_hub_and_resets_initialization(monkeypatch):
    monkeypatch.setattr(
        hub_client,
        "load_client_config",
        lambda: ("https://new-hub.example:9765", "new-secret"),
    )
    monkeypatch.setattr(hub_client, "HUB", "http://old-hub:8765")
    monkeypatch.setattr(hub_client, "TOKEN", "old-secret")
    monkeypatch.setattr(hub_client, "_initialized", True)

    result = hub_client.reload_config()

    assert result == {
        "hub": "https://new-hub.example:9765",
        "token_configured": True,
    }
    assert hub_client.HUB == "https://new-hub.example:9765"
    assert hub_client.TOKEN == "new-secret"
    assert hub_client._initialized is False


@pytest.mark.parametrize(
    "hub,allowed",
    [
        ("http://127.0.0.1:8765", True),
        ("http://[::1]:8765", True),
        ("http://localhost:8765", True),
        ("https://team.example:9765", False),
        ("http://10.0.0.8:8765", False),
        ("http://192.168.1.8:8765", False),
        ("http://[fd00::8]:8765", False),
    ],
)
def test_only_loopback_hub_can_trigger_local_actions(monkeypatch, hub, allowed):
    monkeypatch.setattr(hub_client, "HUB", hub)

    assert hub_client.allows_local_actions() is allowed


def test_response_data_joins_multiline_sse_data():
    raw = 'event: message\ndata: {"jsonrpc":"2.0",\ndata: "result":{"ok":true}}\n\n'

    assert hub_client._response_data(raw) == (
        '{"jsonrpc":"2.0",\n"result":{"ok":true}}'
    )


def test_response_data_keeps_plain_json():
    raw = '{"jsonrpc":"2.0","result":{}}'

    assert hub_client._response_data(raw) == raw


def test_mcp_calls_use_stateless_api_mount(monkeypatch):
    calls = []

    class Response:
        text = '{"jsonrpc":"2.0","id":1,"result":{}}'

        @staticmethod
        def raise_for_status():
            return None

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, json, headers):
            calls.append((url, json["method"], headers["Authorization"]))
            return Response()

    monkeypatch.setattr(hub_client, "HUB", "http://hub")
    monkeypatch.setattr(hub_client, "TOKEN", "secret")
    monkeypatch.setattr(hub_client.httpx, "Client", Client)
    assert hub_client._call("initialize", {}) == {}
    assert calls == [("http://hub/api/", "initialize", "Bearer secret")]


def test_status_requires_token_without_connecting(monkeypatch):
    monkeypatch.setattr(hub_client, "TOKEN", "")
    monkeypatch.setattr(
        hub_client.socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not connect")),
    )

    assert hub_client.status()["available"] is False


def test_status_checks_hub_tcp_port(monkeypatch):
    monkeypatch.setattr(hub_client, "TOKEN", "configured")
    monkeypatch.setattr(hub_client, "HUB", "http://127.0.0.1:8765")
    calls = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(
        hub_client.socket,
        "create_connection",
        lambda address, timeout: calls.append((address, timeout)) or Connection(),
    )

    assert hub_client.status()["available"] is True
    assert calls == [(('127.0.0.1', 8765), 1)]


def test_status_parses_team_hub_host_port(monkeypatch):
    monkeypatch.setattr(hub_client, "TOKEN", "configured")
    monkeypatch.setattr(hub_client, "HUB", "http://team-server:9765")
    calls = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(
        hub_client.socket,
        "create_connection",
        lambda address, timeout: calls.append((address, timeout)) or Connection(),
    )

    assert hub_client.status()["available"] is True
    assert calls == [(('team-server', 9765), 1)]


def test_human_api_forwards_ephemeral_jwt_to_fixed_hub_path(monkeypatch):
    calls = []

    class Response:
        is_error = False
        status_code = 200

        @staticmethod
        def json():
            return {"projects": []}

    class Client:
        def __init__(self, timeout):
            assert timeout == 30

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def request(self, method, url, json, headers):
            calls.append((method, url, json, headers))
            return Response()

    monkeypatch.setattr(hub_client, "HUB", "http://127.0.0.1:8765")
    monkeypatch.setattr(hub_client, "TEAM_HUB_URL", "https://team.example:9765")
    monkeypatch.setattr(hub_client.httpx, "Client", Client)

    result = hub_client.human_api(
        "GET", "/hub/api/projects", "Bearer human.jwt"
    )

    assert result == {"projects": []}
    assert calls == [
        (
            "GET",
            "https://team.example:9765/hub/api/projects",
            None,
            {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer human.jwt",
            },
        )
    ]


def test_saved_team_endpoints_override_legacy_environment(monkeypatch):
    monkeypatch.setattr(hub_client, "TEAM_HUB_URL", "https://legacy.example")
    monkeypatch.setattr(
        hub_client, "HUMAN_AUTH_URL", "https://legacy.example/human-auth",
    )
    settings.update({
        "team_hub_url": "http://127.0.0.1:9765",
        "human_auth_url": "http://127.0.0.1:9766",
    })

    assert hub_client.public_team_config() == {
        "team_hub": "http://127.0.0.1:9765",
        "human_auth": "http://127.0.0.1:9766",
    }


def test_human_api_rejects_missing_jwt_and_non_human_path(monkeypatch):
    monkeypatch.setattr(
        hub_client.httpx,
        "Client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not connect")
        ),
    )

    with pytest.raises(ValueError, match="Human JWT"):
        hub_client.human_api("GET", "/hub/api/projects", "")
    with pytest.raises(ValueError, match="路径无效"):
        hub_client.human_api("GET", "/mcp/", "Bearer human.jwt")


def test_human_api_rejects_plain_http_public_team_hub(monkeypatch):
    monkeypatch.setattr(hub_client, "TEAM_HUB_URL", "http://8.8.8.8:8765")
    monkeypatch.setattr(
        hub_client.httpx,
        "Client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not connect")
        ),
    )

    with pytest.raises(ValueError, match="本机/私网 HTTP 或 HTTPS"):
        hub_client.human_api("GET", "/hub/api/projects", "Bearer human.jwt")


def test_human_api_allows_plain_http_private_team_hub(monkeypatch):
    calls = []

    class Response:
        is_error = False

        @staticmethod
        def json():
            return []

    class Client:
        def __init__(self, **kwargs):
            calls.append(("timeout", kwargs["timeout"]))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def request(self, method, url, json, headers):
            calls.append((method, url, json, headers))
            return Response()

    monkeypatch.setattr(hub_client, "TEAM_HUB_URL", "http://10.18.160.11:8765")
    monkeypatch.setattr(hub_client.httpx, "Client", Client)

    assert hub_client.human_api(
        "GET", "/hub/api/projects", "Bearer human.jwt",
    ) == []
    assert calls[1][0:3] == (
        "GET", "http://10.18.160.11:8765/hub/api/projects", None,
    )


def test_session_lead_reply_uses_capability_without_authorization(monkeypatch):
    calls = []

    class Response:
        is_error = False
        status_code = 201

        @staticmethod
        def json():
            return {"status": "delivered", "message_id": 9, "deliveries": []}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["timeout"] == 30

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, json, headers):
            calls.append((url, json, headers))
            return Response()

    monkeypatch.setattr(hub_client, "TEAM_HUB_URL", "http://10.18.160.11:8765")
    monkeypatch.setattr(hub_client.httpx, "Client", Client)
    payload = {"client_session_id": "session-1", "reply_token": "reply-secret"}

    result = hub_client.session_lead_reply("core", payload)

    assert result["message_id"] == 9
    assert calls[0][0].endswith("/hub/api/projects/core/session-lead/reply")
    assert calls[0][1] == payload
    assert "Authorization" not in calls[0][2]


def test_session_lead_reply_hides_remote_credential_detail(monkeypatch):
    class Response:
        is_error = True
        status_code = 403

        @staticmethod
        def json():
            return {"detail": "token reply-secret belongs to binding 7"}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(hub_client, "TEAM_HUB_URL", "http://10.18.160.11:8765")
    monkeypatch.setattr(hub_client.httpx, "Client", Client)

    with pytest.raises(hub_client.HumanAPIError) as caught:
        hub_client.session_lead_reply(
            "core", {"client_session_id": "session-1", "reply_token": "reply-secret"},
        )

    assert caught.value.status_code == 403
    assert caught.value.detail == "Invalid reply credentials"
    assert "reply-secret" not in str(caught.value)


def test_session_lead_capability_helpers_use_only_fixed_paths(monkeypatch):
    calls = []

    class Response:
        is_error = False
        status_code = 200

        @staticmethod
        def json():
            return {"status": "ok"}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, json, headers):
            calls.append((url, json, headers))
            return Response()

    monkeypatch.setattr(hub_client, "TEAM_HUB_URL", "https://team.example")
    monkeypatch.setattr(hub_client.settings, "get", lambda: {})
    monkeypatch.setattr(hub_client.httpx, "Client", Client)

    hub_client.session_lead_claim("core", {"reply_token": "secret"})
    hub_client.session_lead_status("core", {"reply_token": "secret", "status": "idle"})
    hub_client.session_lead_complete("core", 31, {"claim_token": "claim"})
    hub_client.session_lead_reply("core", {"reply_token": "secret"})

    assert [call[0] for call in calls] == [
        "https://team.example/hub/api/projects/core/session-lead/inbox/claim",
        "https://team.example/hub/api/projects/core/session-lead/status",
        "https://team.example/hub/api/projects/core/session-lead/inbox/31/complete",
        "https://team.example/hub/api/projects/core/session-lead/reply",
    ]
    assert all("Authorization" not in headers for _, _, headers in calls)


def test_human_login_uses_loopback_issuer_and_validates_response(monkeypatch):
    calls = []

    class Response:
        is_error = False

        @staticmethod
        def json():
            return {
                "access_token": "human.jwt",
                "profile": {"username": "fyc", "display_name": "付彦超"},
            }

    class Client:
        def __init__(self, **kwargs):
            calls.append(("timeout", kwargs["timeout"]))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, json):
            calls.append((url, json))
            return Response()

    monkeypatch.setattr(hub_client, "HUMAN_AUTH_URL", "http://127.0.0.1:8766")
    monkeypatch.setattr(hub_client.httpx, "Client", Client)
    result = hub_client.human_login("fyc", "local-secret")
    assert result["access_token"] == "human.jwt"
    assert calls == [
        ("timeout", 10),
        ("http://127.0.0.1:8766/token", {"username": "fyc", "password": "local-secret"}),
    ]


def test_human_login_rejects_cleartext_remote_issuer(monkeypatch):
    monkeypatch.setattr(hub_client, "HUMAN_AUTH_URL", "http://team.example:8766")
    monkeypatch.setattr(
        hub_client.httpx,
        "Client",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not connect")),
    )
    with pytest.raises(ValueError, match="本机/私网 HTTP 或 HTTPS"):
        hub_client.human_login("fyc", "local-secret")


def test_human_account_lifecycle_calls_fixed_remote_issuer_routes(monkeypatch):
    calls = []

    class Response:
        is_error = False

        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    class Client:
        def __init__(self, **kwargs):
            calls.append(("timeout", kwargs["timeout"]))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def request(self, method, url, json, headers):
            calls.append((method, url, json, headers))
            if url.endswith("/register"):
                return Response({"account": {"username": "alice", "status": "pending"}})
            if url.endswith("/admin/invitations"):
                return Response({"invite_code": "one-time", "expires_at": 123})
            return Response({"users": []})

    monkeypatch.setattr(hub_client, "HUMAN_AUTH_URL", "https://team.example/human-auth")
    monkeypatch.setattr(hub_client.httpx, "Client", Client)
    registered = hub_client.human_register(
        "alice", "Alice", "alice-password-123", "one-time"
    )
    changed = hub_client.human_change_password(
        "Bearer human.jwt", "new-password-1234"
    )
    invitation = hub_client.human_create_invitation("Bearer human.jwt", 3600)
    users = hub_client.human_list_users("Bearer human.jwt")

    assert registered["account"]["status"] == "pending"
    assert changed == {"users": []}
    assert invitation["invite_code"] == "one-time"
    assert users == {"users": []}
    assert calls[1] == (
        "POST",
        "https://team.example/human-auth/register",
        {
            "username": "alice",
            "display_name": "Alice",
            "password": "alice-password-123",
            "invite_code": "one-time",
        },
        {"Accept": "application/json"},
    )
    assert calls[3][0:3] == (
        "PATCH",
        "https://team.example/human-auth/me/password",
        {"new_password": "new-password-1234"},
    )
    assert calls[3][3]["Authorization"] == "Bearer human.jwt"
    assert calls[5][0:3] == (
        "POST",
        "https://team.example/human-auth/admin/invitations",
        {"expires_in": 3600},
    )
    assert calls[5][3]["Authorization"] == "Bearer human.jwt"


def test_human_admin_routes_add_private_second_factor_only(monkeypatch, tmp_path):
    token_file = tmp_path / "admin-access-token"
    token_file.write_text(
        "public-admin-factor-0123456789abcdef", encoding="utf-8"
    )
    token_file.chmod(0o600)
    calls = []

    class Response:
        is_error = False

        def json(self):
            return {"users": []}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def request(self, method, url, json, headers):
            calls.append((method, url, json, headers))
            return Response()

    monkeypatch.setattr(hub_client, "HUMAN_AUTH_URL", "https://auth.example")
    monkeypatch.setattr(
        hub_client, "TEAM_ADMIN_ACCESS_TOKEN_FILE", str(token_file)
    )
    monkeypatch.setattr(hub_client.httpx, "Client", Client)

    hub_client.human_profile("Bearer human.jwt")
    hub_client.human_list_users("Bearer human.jwt")

    assert "X-Agent-Hub-Admin-Key" not in calls[0][3]
    assert calls[1][3]["X-Agent-Hub-Admin-Key"] == (
        "public-admin-factor-0123456789abcdef"
    )


def test_team_invitation_lifecycle_uses_fixed_admin_route(monkeypatch):
    calls = []

    class Response:
        is_error = False

        def json(self):
            return {"invitation": None}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def request(self, method, url, json, headers):
            calls.append((method, url, json, headers))
            return Response()

    monkeypatch.setattr(
        hub_client, "HUMAN_AUTH_URL", "https://team.example/human-auth"
    )
    monkeypatch.setattr(hub_client.httpx, "Client", Client)

    hub_client.human_get_team_invitation("Bearer human.jwt")
    hub_client.human_create_team_invitation("Bearer human.jwt", None)
    hub_client.human_update_team_invitation("Bearer human.jwt", 3600)
    hub_client.human_revoke_team_invitation("Bearer human.jwt")

    assert [(method, url, payload) for method, url, payload, _ in calls] == [
        ("GET", "https://team.example/human-auth/admin/team-invitation", None),
        (
            "POST",
            "https://team.example/human-auth/admin/team-invitation",
            {"expires_in": None},
        ),
        (
            "PATCH",
            "https://team.example/human-auth/admin/team-invitation",
            {"expires_in": 3600},
        ),
        ("DELETE", "https://team.example/human-auth/admin/team-invitation", None),
    ]


def test_human_admin_route_rejects_readable_second_factor_file(monkeypatch, tmp_path):
    token_file = tmp_path / "admin-access-token"
    token_file.write_text(
        "public-admin-factor-0123456789abcdef", encoding="utf-8"
    )
    token_file.chmod(0o644)
    monkeypatch.setattr(
        hub_client, "TEAM_ADMIN_ACCESS_TOKEN_FILE", str(token_file)
    )

    with pytest.raises(ValueError, match="0600"):
        hub_client.human_list_users("Bearer human.jwt")
