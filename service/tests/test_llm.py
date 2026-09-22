"""The single model call: goes to OpenRouter's chat completions with a strict JSON schema."""
import json
import os

os.environ.setdefault("OPENROUTER_API_KEY", "")

import httpx

from app import llm as llm_mod
from app.config import settings
from app.responder import classify_intent


def _mock_openai(captured):
    from openai import OpenAI

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "cmpl", "object": "chat.completion", "created": 0, "model": "test",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": json.dumps({"intent": "delegate", "confidence": 0.93})}}],
            },
        )

    return OpenAI(api_key="test-key", base_url=settings.llm_base_url, http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_no_key_means_no_client_and_none(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "")

    def boom():
        raise AssertionError("client must not be built without a key")

    monkeypatch.setattr(llm_mod, "_client", boom)
    assert llm_mod.structured_json(label="t", system="s", user="u", schema_name="x", schema={"type": "object"}) is None


def test_defaults_point_at_openrouter():
    assert settings.llm_base_url == "https://openrouter.ai/api/v1"
    assert settings.llm_model == "z-ai/glm-5.3-flash"


def test_structured_call_shape_and_parsing(monkeypatch):
    captured = {}
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_model", "openai/gpt-6-astra")
    monkeypatch.setattr(llm_mod, "_client", lambda: _mock_openai(captured))

    assert classify_intent("Can my roommate pick up my shirt for me?") == "delegate"

    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer test-key"
    body = captured["body"]
    assert body["model"] == "openai/gpt-6-astra"
    assert body["messages"][0]["role"] == "system" and body["messages"][1]["role"] == "user"
    rf = body["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["name"] == "intent" and rf["json_schema"]["strict"] is True
    prov = body["provider"]
    assert prov["require_parameters"] is True and prov["zdr"] is True and prov["data_collection"] == "deny"
    assert prov["only"] == ["coreweave", "fireworks", "baseten"] and prov["order"] == prov["only"]


def test_provider_failure_degrades_to_none(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "test-key")

    def failing_client():
        from openai import OpenAI

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})

        return OpenAI(api_key="k", base_url=settings.llm_base_url, http_client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=0)

    monkeypatch.setattr(llm_mod, "_client", failing_client)
    assert classify_intent("hello") is None
