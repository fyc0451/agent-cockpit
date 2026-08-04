import hub_client


def _write_client_env(monkeypatch, tmp_path, content: str) -> None:
    env_file = tmp_path / "client.env"
    env_file.write_text(content)
    monkeypatch.setattr(hub_client, "_CLIENT_ENV", env_file)


def test_load_config_defaults_to_localhost_without_client_env(monkeypatch, tmp_path):
    env_file = tmp_path / "client.env"
    assert not env_file.exists()
    monkeypatch.setattr(hub_client, "_CLIENT_ENV", env_file)

    assert hub_client._load_config() == ("http://127.0.0.1:8765", "")


def test_load_config_uses_client_env_hub_for_shared_team_hub(monkeypatch, tmp_path):
    _write_client_env(
        monkeypatch,
        tmp_path,
        "hub=http://team-server:8765\ntoken=secret123\n",
    )

    assert hub_client._load_config() == ("http://team-server:8765", "secret123")


def test_load_config_keeps_localhost_when_hub_unset(monkeypatch, tmp_path):
    _write_client_env(monkeypatch, tmp_path, "token=abc\n")

    assert hub_client._load_config() == ("http://127.0.0.1:8765", "abc")


def test_load_config_ignores_comments_and_empty_hub(monkeypatch, tmp_path):
    _write_client_env(
        monkeypatch,
        tmp_path,
        "# hub=http://ignored.example:8765\nhub=\ntoken=xyz\n",
    )

    assert hub_client._load_config() == ("http://127.0.0.1:8765", "xyz")


def test_response_data_joins_multiline_sse_data():
    raw = 'event: message\ndata: {"jsonrpc":"2.0",\ndata: "result":{"ok":true}}\n\n'

    assert hub_client._response_data(raw) == (
        '{"jsonrpc":"2.0",\n"result":{"ok":true}}'
    )


def test_response_data_keeps_plain_json():
    raw = '{"jsonrpc":"2.0","result":{}}'

    assert hub_client._response_data(raw) == raw


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
