import pytest

from app.llm import ModelError, parse_json


@pytest.mark.parametrize("text", [
    '{"a": 1}',
    '```json\n{"a": 1}\n```',
    '```\n{"a": 1}\n```',
    'Here you go:\n{"a": 1}\nHope that helps.',
])
def test_parse_json_finds_the_object(text):
    assert parse_json(text) == {"a": 1}


@pytest.mark.parametrize("text", ["no json here", '{"a": 1', "[1, 2]", ""])
def test_parse_json_rejects_bad_answers(text):
    with pytest.raises(ModelError) as e:
        parse_json(text)
    assert e.value.code == "invalid_json"


def test_anthropic_key_is_stripped_of_stray_line_breaks(monkeypatch):
    from app.llm import AnthropicModel
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key\n")
    assert AnthropicModel()._client.api_key == "sk-ant-test-key"


def test_connection_errors_name_the_cause_without_leaking_the_key(monkeypatch):
    import anthropic, httpx
    from app.llm import AnthropicModel
    model = AnthropicModel(api_key="sk-ant-secret-value")

    def boom(**kw):
        try:
            raise httpx.LocalProtocolError("Illegal header value b'sk-ant-secret-value'")
        except httpx.LocalProtocolError as cause:
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")) from cause
    monkeypatch.setattr(model._client.messages, "create", boom)
    with pytest.raises(ModelError) as e:
        model.json("hi")
    assert "LocalProtocolError" in str(e.value)
    assert "sk-ant-secret-value" not in str(e.value)


def test_sdk_does_no_hidden_retries():
    from app.llm import AnthropicModel
    assert AnthropicModel(api_key="k")._client.max_retries == 0     # retries are ours, so they're timed and logged
