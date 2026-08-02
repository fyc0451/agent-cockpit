import hub_client


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
