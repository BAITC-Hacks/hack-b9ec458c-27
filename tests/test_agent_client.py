import sys
from types import SimpleNamespace

import pytest

from replenishment.agent_client import OpenAIChatModel, model_from_env


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("AGENT_API_KEY", "AGENT_MODEL", "AGENT_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


def test_absent_configuration_never_constructs_client(clean_env):
    assert model_from_env() is None


def test_missing_model_does_not_choose_one(clean_env, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "test-placeholder")
    assert model_from_env() is None


def test_factory_uses_explicit_config_and_bounds_requests(clean_env, monkeypatch):
    made = []
    def factory(**kwargs):
        made.append(kwargs)
        return SimpleNamespace(close=lambda: None)
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=factory))
    monkeypatch.setenv("AGENT_API_KEY", "test-placeholder")
    monkeypatch.setenv("AGENT_MODEL", "test-model")
    model = model_from_env()
    assert model.model == "test-model"
    assert made == [dict(api_key="test-placeholder", base_url="https://api.openai.com/v1",
                         timeout=20, max_retries=0)]
    model.close()


@pytest.mark.parametrize("url", ["http://example.com/v1", "https://user:password@example.com",
                                 "https://example.com?key=secret", "https://example.com#secret"])
def test_invalid_endpoint_rejected(clean_env, monkeypatch, url):
    monkeypatch.setenv("AGENT_API_KEY", "test-placeholder")
    monkeypatch.setenv("AGENT_MODEL", "test-model")
    monkeypatch.setenv("AGENT_BASE_URL", url)
    with pytest.raises(ValueError, match="HTTPS API endpoint"):
        model_from_env()


def test_adapter_serializes_tool_protocol_and_converts_reply():
    requests = []
    def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="tool_calls", message=SimpleNamespace(
            refusal=None, content=None, tool_calls=[SimpleNamespace(id="one", function=SimpleNamespace(
                name="calculate_replenishment", arguments="{}"))]))])
    model = OpenAIChatModel(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), "test-model")
    response = model.complete([{"role": "user", "content": "Test"}], [{"type": "function"}])
    assert response.calls[0].name == "calculate_replenishment"
    assert response.calls[0].arguments == "{}"
    assert requests[0]["parallel_tool_calls"] is False
    assert requests[0]["max_completion_tokens"] == 1200
    assert requests[0]["model"] == "test-model"


@pytest.mark.parametrize("finish,refusal", [("length", None), ("content_filter", None), ("stop", "Refused")])
def test_incomplete_or_refused_response_is_error(finish, refusal):
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw:
        SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(refusal=refusal))]))))
    with pytest.raises(ValueError, match="did not complete"):
        OpenAIChatModel(client, "test-model").complete([], [])
