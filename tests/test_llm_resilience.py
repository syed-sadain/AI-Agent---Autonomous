"""Tests for the mandatory improvement: retry, self-repair, provider fallback, circuit breaker."""
import asyncio
import json

import httpx
import pytest
from pydantic import BaseModel

from app.llm.base import AllProvidersFailedError, PermanentLLMError, TransientLLMError
from app.llm.jsonutil import extract_json
from app.llm.openai_compat import OpenAICompatProvider
from app.llm.resilient import ResilientLLM


class Out(BaseModel):
    answer: str


class Scripted:
    """Provider that replays a script of outputs/exceptions."""

    always_available = False

    def __init__(self, name, script):
        self.name, self.script, self.calls, self.seen = name, list(script), 0, []

    async def chat(self, messages, *, purpose, json_mode=True, temperature=0.3):
        self.calls += 1
        self.seen.append(messages)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self):
        pass


def llm(*providers, **kw):
    return ResilientLLM(list(providers), backoff_base_s=0, **kw)


def run(coro):
    return asyncio.run(coro)


def call(client):
    events = []
    out = run(client.complete_json(purpose="t", system="s", user="u", model_cls=Out, events=events))
    return out, events


def test_extract_json_handles_fences_and_trailing_commas():
    assert extract_json('Sure!\n```json\n{"a": [1,2,],}\n```') == {"a": [1, 2]}
    assert extract_json('noise {"a": 1} noise') == {"a": 1}


def test_retries_transient_errors_then_succeeds():
    p = Scripted("p", [TransientLLMError("429"), TransientLLMError("503"), '{"answer": "ok"}'])
    out, events = call(llm(p, max_retries=3))
    assert out.answer == "ok" and p.calls == 3
    assert events[-1].ok and events[-1].attempts == 3


def test_invalid_json_is_fed_back_for_self_repair():
    p = Scripted("p", ["definitely not json", '{"answer": "fixed"}'])
    out, _ = call(llm(p))
    assert out.answer == "fixed"
    assert "invalid" in p.seen[1][-1]["content"].lower()  # model was shown its mistake


def test_schema_violation_triggers_repair():
    p = Scripted("p", ['{"wrong": 1}', '{"answer": "ok"}'])
    out, _ = call(llm(p))
    assert out.answer == "ok"


def test_falls_back_to_next_provider_on_permanent_error():
    a, b = Scripted("a", [PermanentLLMError("401")]), Scripted("b", ['{"answer": "from b"}'])
    out, events = call(llm(a, b))
    assert out.answer == "from b" and a.calls == 1  # permanent errors are not retried
    assert [e.ok for e in events] == [False, True]


def test_all_providers_failing_raises():
    a = Scripted("a", [TransientLLMError("x")] * 3)
    with pytest.raises(AllProvidersFailedError):
        call(llm(a, max_retries=3))


def test_circuit_breaker_skips_unhealthy_provider():
    a = Scripted("a", [PermanentLLMError("down")] * 2)
    b = Scripted("b", ['{"answer": "1"}', '{"answer": "2"}'])
    client = llm(a, b, circuit_threshold=1, circuit_cooldown_s=60)
    call(client)
    call(client)
    assert a.calls == 1  # second request never touched the tripped provider


def test_http_provider_retries_429_then_succeeds():
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        if hits["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        body = {"choices": [{"message": {"content": json.dumps({"answer": "http ok"})}}]}
        return httpx.Response(200, json=body)

    provider = OpenAICompatProvider("groq", "http://test/v1", "m", "key")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out, events = call(llm(provider))
    assert out.answer == "http ok" and hits["n"] == 2 and events[-1].attempts == 2


def test_connection_refused_is_not_retried():
    def handler(request):
        raise httpx.ConnectError("refused")

    provider = OpenAICompatProvider("ollama", "http://localhost:1/v1", "m")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ok = Scripted("b", ['{"answer": "b"}'])
    out, events = call(llm(provider, ok, max_retries=3))
    assert out.answer == "b" and events[0].attempts == 1
