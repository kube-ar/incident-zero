"""The LLM layer sends each provider the request it expects, without any network or key."""

from types import SimpleNamespace

import anthropic
import httpx2
import openai
import pytest

from oncall import llm as llm_mod
from oncall.llm import LLMError, get_llm

SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("ONCALL_LLM_PROVIDER", "ONCALL_LLM_MODEL", "ONCALL_LLM_EFFORT", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
              "FIREWORKS_API_KEY", "OPENROUTER_API_KEY"):  # fmt: skip
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(llm_mod, "_load_env", lambda: None)
    monkeypatch.setenv("ONCALL_LLM_CACHE", "0")


class FakeClaude:
    def __init__(self, **kwargs):
        self.kwargs, self.calls = kwargs, []
        reply = SimpleNamespace(
            content=[SimpleNamespace(type="text", text='{"ok": true}')],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=10, output_tokens=3),
        )

        def create(**request):
            self.calls.append(request)
            return reply

        self.messages = SimpleNamespace(create=create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


def test_claude_json_uses_structured_output_and_fallbacks(monkeypatch):
    made = []
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: made.append(FakeClaude(**kw)) or made[-1])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    llm = get_llm("anthropic")
    assert llm.model == "claude-opus-5-5"
    assert llm.json("q", SCHEMA, system="be brief") == {"ok": True}
    request = made[0].calls[0]
    assert request["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert request["fallbacks"] == "default" and request["betas"] == ["server-side-fallback-2026-07-01"]
    assert request["system"] == "be brief" and request["cache_control"] == {"type": "ephemeral"}
    assert made[0].kwargs == {"api_key": "test-key"}
    assert llm.usage.calls == 1 and llm.usage.input_tokens == 10


def test_claude_model_without_fallbacks(monkeypatch):
    made = []
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: made.append(FakeClaude(**kw)) or made[-1])
    llm = get_llm("anthropic", "claude-haiku-5-5")
    llm.text("hi")
    assert "fallbacks" not in made[0].calls[0] and made[0].kwargs == {}


def test_claude_refusal_is_an_error(monkeypatch):
    fake = FakeClaude()
    fake.messages.create = lambda **r: SimpleNamespace(
        content=[], stop_reason="refusal", usage=SimpleNamespace(input_tokens=1, output_tokens=0)
    )
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: fake)
    with pytest.raises(LLMError, match="declined"):
        get_llm("anthropic", "claude-haiku-5-5").text("hi")


class FakeOpenAI:
    def __init__(self, fail_schema=False, **kwargs):
        self.kwargs, self.calls, self.fail_schema = kwargs, [], fail_schema

        def create(**request):
            self.calls.append(request)
            if self.fail_schema and request.get("response_format", {}).get("type") == "json_schema":
                response = httpx2.Response(400, request=httpx2.Request("POST", "https://example.test"))
                raise openai.BadRequestError("json_schema not supported", response=response, body=None)
            message = SimpleNamespace(content='```json\n{"ok": true}\n```', refusal=None)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message, finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=7, completion_tokens=2),
            )

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


@pytest.mark.parametrize(
    "provider, key, base_url",
    [
        ("openai", "OPENAI_API_KEY", None),
        ("fireworks", "FIREWORKS_API_KEY", "https://api.fireworks.ai/inference/v1"),
        ("openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1"),
    ],
)
def test_openai_compatible_providers(monkeypatch, provider, key, base_url):
    made = []
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: made.append(FakeOpenAI(**kw)) or made[-1])
    monkeypatch.setenv(key, "test-key")
    llm = get_llm(provider, "some/model")
    assert llm.json("q", SCHEMA, system="s") == {"ok": True}
    assert made[0].kwargs == {"api_key": "test-key", "base_url": base_url}
    request = made[0].calls[0]
    assert request["response_format"]["type"] == "json_schema"
    assert request["response_format"]["json_schema"]["schema"] == SCHEMA
    assert request["messages"][0] == {"role": "system", "content": "s"}


def test_falls_back_to_json_mode_when_schema_is_refused(monkeypatch):
    made = []
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: made.append(FakeOpenAI(fail_schema=True, **kw)) or made[-1])
    monkeypatch.setenv("FIREWORKS_API_KEY", "k")
    assert get_llm("fireworks", "m").json("q", SCHEMA) == {"ok": True}
    retry = made[0].calls[1]
    assert retry["response_format"] == {"type": "json_object"} and "JSON Schema" in retry["messages"][-1]["content"]


def test_setup_errors_say_what_to_do(monkeypatch):
    with pytest.raises(LLMError, match="ONCALL_LLM_PROVIDER"):
        get_llm()
    with pytest.raises(LLMError, match="unknown|Unknown"):
        get_llm("bard")
    with pytest.raises(LLMError, match="ONCALL_LLM_MODEL"):
        get_llm("openai")
    with pytest.raises(LLMError, match="OPENAI_API_KEY"):
        get_llm("openai", "m").text("hi")


def test_effort_only_applies_to_claude(monkeypatch):
    monkeypatch.setenv("ONCALL_LLM_EFFORT", "high")
    assert get_llm("anthropic").effort == "high"
    assert get_llm("openai", "m").effort is None


def test_conversation_loop_on_claude(monkeypatch):
    from oncall.llm import Conversation

    replies = [
        SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking=""),
                                 SimpleNamespace(type="tool_use", id="t1", name="lookup", input={"q": "x"})],
                        stop_reason="tool_use", usage=SimpleNamespace(input_tokens=5, output_tokens=2)),
        SimpleNamespace(content=[SimpleNamespace(type="text", text="done")], stop_reason="end_turn",
                        usage=SimpleNamespace(input_tokens=5, output_tokens=1)),
    ]  # fmt: skip
    sent = []

    class Fake:
        def __init__(self, **kw):
            def create(**request):
                sent.append(request)
                return replies[len(sent) - 1]

            self.messages = SimpleNamespace(create=create)
            self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))

    monkeypatch.setattr(anthropic, "Anthropic", Fake)
    tools = [{"name": "lookup", "description": "d", "parameters": {"type": "object", "properties": {}}}]
    convo = Conversation(get_llm("anthropic"), tools, system="s")
    turn = convo.send("go")
    assert [c.name for c in turn.tool_calls] == ["lookup"] and turn.tool_calls[0].arguments == {"q": "x"}
    turn = convo.reply({"t1": {"rows": 1}}, errors={"t1"})
    assert turn.done and turn.text == "done"
    assert sent[0]["tools"][0]["input_schema"] == tools[0]["parameters"]
    # the assistant's own blocks go back unchanged (thinking included), then one message with all results
    assert sent[1]["messages"][1]["content"] is replies[0].content
    assert sent[1]["messages"][2]["content"][0] == {"type": "tool_result", "tool_use_id": "t1", "content": '{"rows": 1}',
                                                    "is_error": True}  # fmt: skip


def test_conversation_loop_on_openai_compatible(monkeypatch):
    from oncall.llm import Conversation

    def message(content, calls):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls))],
                               usage=None)  # fmt: skip

    call = SimpleNamespace(id="c1", function=SimpleNamespace(name="lookup", arguments='{"q": "x"}'))
    replies = [message(None, [call]), message("done", None)]
    sent = []

    class Fake:
        def __init__(self, **kw):
            def create(**request):
                sent.append(request)
                return replies[len(sent) - 1]

            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    monkeypatch.setattr(openai, "OpenAI", Fake)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    tools = [{"name": "lookup", "description": "d", "parameters": {"type": "object", "properties": {}}}]
    convo = Conversation(get_llm("openrouter", "some/model"), tools, system="s")
    turn = convo.send("go")
    assert turn.tool_calls[0].arguments == {"q": "x"}
    turn = convo.reply({"c1": "ok"})
    assert turn.done
    assert sent[0]["tools"][0]["function"]["name"] == "lookup"
    assert sent[1]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "ok"}
    assert sent[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"] == '{"q": "x"}'
