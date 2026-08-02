import hub_client


def test_response_data_joins_multiline_sse_data():
    raw = 'event: message\ndata: {"jsonrpc":"2.0",\ndata: "result":{"ok":true}}\n\n'

    assert hub_client._response_data(raw) == (
        '{"jsonrpc":"2.0",\n"result":{"ok":true}}'
    )


def test_response_data_keeps_plain_json():
    raw = '{"jsonrpc":"2.0","result":{}}'

    assert hub_client._response_data(raw) == raw
