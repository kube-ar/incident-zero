"""One small interface to the LLM providers learners use: Claude, OpenAI, Fireworks and OpenRouter.

Pick one in `.env` (copy `.env.example` to `.env`; `.env` is gitignored, never commit a key):

    ONCALL_LLM_PROVIDER=anthropic     # anthropic | openai | fireworks | openrouter
    ONCALL_LLM_MODEL=                 # optional for anthropic, required for the others
    ANTHROPIC_API_KEY=...            # the key of the provider you picked

Then:

    from oncall.llm import get_llm
    llm = get_llm()
    llm.text("Summarize this statement: ...")            # -> str
    llm.json("Extract the claims: ...", schema={...})    # -> dict matching the JSON schema

Claude goes through the official `anthropic` SDK. OpenAI, Fireworks and OpenRouter go through
the `openai` SDK, pointed at each provider's OpenAI-compatible endpoint. `uv run oncall llm`
checks your setup with two tiny calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import ROOT


class LLMError(RuntimeError):
    """Anything that stops a call: setup, credentials, the provider, or an answer that isn't usable."""


@dataclass(frozen=True)
class Provider:
    name: str
    key_env: str
    base_url: str | None
    default_model: str | None
    keys_at: str


PROVIDERS = {
    p.name: p
    for p in [
        Provider("anthropic", "ANTHROPIC_API_KEY", None, "claude-opus-5-5", "https://platform.claude.com"),
        Provider("openai", "OPENAI_API_KEY", None, None, "https://platform.openai.com"),
        Provider(
            "fireworks", "FIREWORKS_API_KEY", "https://api.fireworks.ai/inference/v1", None, "https://fireworks.ai"
        ),
        Provider("openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", None, "https://openrouter.ai"),
    ]
}

# Claude models that take a server-side fallback when a safety classifier declines a request.
CLAUDE_FALLBACKS = {"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
MAX_TOKENS = 16000


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    load_dotenv(ROOT / ".env")
    load_dotenv()  # and a .env in the current folder, if you run from elsewhere
    # An empty KEY= line from the template must not count as a key: it would shadow other credentials.
    for p in PROVIDERS.values():
        if os.environ.get(p.key_env) == "":
            del os.environ[p.key_env]


@dataclass
class Usage:
    calls: int = 0
    cached: int = 0  # answers read from .oncall-cache instead of calling the provider
    input_tokens: int = 0
    output_tokens: int = 0
    contexts: list[int] = field(default_factory=list)  # characters of conversation each Conversation request sent


@dataclass
class LLM:
    provider: Provider
    model: str
    effort: str | None = None  # Claude only: low | medium | high | xhigh | max
    usage: Usage = field(default_factory=Usage)
    _client: Any = None

    def __repr__(self) -> str:
        return f"<LLM {self.provider.name}:{self.model}>"

    # ── Public calls ─────────────────────────────────────────────────────────

    def text(self, prompt: str, system: str | None = None) -> str:
        """One answer as plain text."""
        return self._call(prompt, system, None)

    def json(self, prompt: str, schema: dict, system: str | None = None) -> dict:
        """One answer as JSON that follows `schema` (a JSON Schema object).

        Use `"additionalProperties": false` and list every property in `required`: OpenAI's strict
        mode needs both, and every provider here accepts them.
        """
        raw = self._call(prompt, system, schema)
        try:
            value = json.loads(_strip_fences(raw))
        except json.JSONDecodeError as e:
            raise LLMError(f"{self}: the answer isn't valid JSON ({e.msg}): {raw[:200]!r}") from e
        if not isinstance(value, dict):
            raise LLMError(f"{self}: expected a JSON object, got {type(value).__name__}")
        return value

    # ── Providers ────────────────────────────────────────────────────────────

    def _call(self, prompt: str, system: str | None, schema: dict | None) -> str:
        """Answers are cached on disk (.oncall-cache/llm), so rerunning the checks costs nothing.

        Set ONCALL_LLM_CACHE=0 to always call the provider.
        """
        use_cache = os.environ.get("ONCALL_LLM_CACHE", "1") != "0"
        key = hashlib.sha256(
            json.dumps([self.provider.name, self.model, self.effort, system, prompt, schema], sort_keys=True).encode()
        ).hexdigest()
        path = ROOT / ".oncall-cache" / "llm" / f"{key}.json"
        if use_cache and path.exists():
            self.usage.cached += 1
            return json.loads(path.read_text(encoding="utf-8"))["answer"]
        if self.provider.name == "anthropic":
            answer = self._claude(prompt, system, schema)
        else:
            answer = self._openai_compatible(prompt, system, schema)
        if use_cache:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"model": self.model, "answer": answer}), encoding="utf-8")
        return answer

    def _claude(self, prompt: str, system: str | None, schema: dict | None) -> str:
        import anthropic

        if self._client is None:
            key = os.environ.get(self.provider.key_env)
            # Without a key, the SDK still finds an `ant auth login` profile if there is one.
            self._client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            request["system"] = system
            request["cache_control"] = {"type": "ephemeral"}  # repeated system prompts are read from cache
        output_config: dict[str, Any] = {}
        if self.effort:
            output_config["effort"] = self.effort
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if output_config:
            request["output_config"] = output_config
        try:
            if self.model in CLAUDE_FALLBACKS:
                # If a safety classifier declines, the API retries on a fallback model in the same call.
                response = self._client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **request
                )
            else:
                response = self._client.messages.create(**request)
        except anthropic.AuthenticationError as e:
            raise LLMError(f"{self}: the key in {self.provider.key_env} was rejected") from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"{self}: no model {self.model!r} (check ONCALL_LLM_MODEL)") from e
        except anthropic.RateLimitError as e:
            raise LLMError(f"{self}: rate limited, try again in a minute") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"{self}: the API answered {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"{self}: can't reach the API ({e})") from e
        self._count(response.usage.input_tokens, response.usage.output_tokens)
        if response.stop_reason == "refusal":
            raise LLMError(f"{self}: the model declined this request")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"{self}: the answer hit the {MAX_TOKENS}-token limit before it finished")
        return "".join(block.text for block in response.content if block.type == "text")

    def _openai_compatible(self, prompt: str, system: str | None, schema: dict | None) -> str:
        import openai

        if self._client is None:
            key = os.environ.get(self.provider.key_env)
            if not key:
                raise LLMError(
                    f"Set {self.provider.key_env} in .env for provider {self.provider.name!r} "
                    f"(get a key at {self.provider.keys_at})"
                )
            self._client = openai.OpenAI(api_key=key, base_url=self.provider.base_url)
        messages = [{"role": "system", "content": system}] if system else []
        messages.append({"role": "user", "content": prompt})
        request: dict[str, Any] = {"model": self.model, "messages": messages}
        if schema:
            request["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": schema, "strict": True},
            }
        try:
            try:
                response = self._client.chat.completions.create(**request)
            except openai.BadRequestError:
                if not schema:
                    raise
                # Some models behind Fireworks or OpenRouter don't take a JSON schema: ask in words instead.
                request["response_format"] = {"type": "json_object"}
                request["messages"] = [
                    *messages[:-1],
                    {"role": "user", "content": f"{prompt}\n\nAnswer with JSON only, following this JSON Schema:\n"
                     f"{json.dumps(schema)}"},
                ]  # fmt: skip
                response = self._client.chat.completions.create(**request)
        except openai.AuthenticationError as e:
            raise LLMError(f"{self}: the key in {self.provider.key_env} was rejected") from e
        except openai.NotFoundError as e:
            raise LLMError(f"{self}: no model {self.model!r} at {self.provider.name} (check ONCALL_LLM_MODEL)") from e
        except openai.RateLimitError as e:
            raise LLMError(f"{self}: rate limited, try again in a minute") from e
        except openai.APIStatusError as e:
            raise LLMError(f"{self}: the API answered {e.status_code}: {e.message}") from e
        except openai.APIConnectionError as e:
            raise LLMError(f"{self}: can't reach {self.provider.base_url or 'the API'} ({e})") from e
        if response.usage:
            self._count(response.usage.prompt_tokens, response.usage.completion_tokens)
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise LLMError(f"{self}: the answer was cut off at the model's length limit")
        if getattr(choice.message, "refusal", None):
            raise LLMError(f"{self}: the model declined this request")
        return choice.message.content or ""

    def _count(self, input_tokens: int | None, output_tokens: int | None) -> None:
        self.usage.calls += 1
        self.usage.input_tokens += input_tokens or 0
        self.usage.output_tokens += output_tokens or 0


def _strip_fences(text: str) -> str:
    """Some models wrap JSON in ```json fences even when asked not to."""
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    return match.group(1) if match else text.strip()


def get_llm(provider: str | None = None, model: str | None = None) -> LLM:
    """The LLM configured in .env (or the provider and model you pass)."""
    _load_env()
    name = (provider or os.environ.get("ONCALL_LLM_PROVIDER") or "").strip().lower()
    if not name:
        raise LLMError("Set ONCALL_LLM_PROVIDER in .env: copy .env.example to .env and pick a provider")
    if name not in PROVIDERS:
        raise LLMError(f"Unknown ONCALL_LLM_PROVIDER {name!r}: use one of {', '.join(PROVIDERS)}")
    p = PROVIDERS[name]
    chosen = (model or os.environ.get("ONCALL_LLM_MODEL") or p.default_model or "").strip()
    if not chosen:
        raise LLMError(f"Set ONCALL_LLM_MODEL in .env: {name} has no default model (use an id from its model list)")
    effort = (os.environ.get("ONCALL_LLM_EFFORT") or "").strip() or None
    if effort and name != "anthropic":
        effort = None  # an effort setting only applies to Claude
    return LLM(p, chosen, effort)


# ── Conversations with tools (the agent milestones) ──────────────────────────


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Turn:
    text: str
    tool_calls: list[ToolCall]

    @property
    def done(self) -> bool:
        """The model answered without asking for a tool."""
        return not self.tool_calls


class Conversation:
    """A tool-using conversation with the configured model.

        convo = Conversation(get_llm(), tools=[{"name": ..., "description": ..., "parameters": {...}}], system="...")
        turn = convo.send("What broke?")
        while not turn.done:
            turn = convo.reply({call.id: run_tool(call.name, call.arguments) for call in turn.tool_calls})

    Tools are described once, in JSON Schema, for every provider. The history stays in the
    provider's own format, so the model's own blocks (Claude's thinking included) go back to it
    unchanged. Results of one turn's tool calls go back together, in one message.
    """

    def __init__(self, llm: LLM, tools: list[dict], system: str | None = None):
        self.llm, self.tools, self.system = llm, tools, system
        self.messages: list[Any] = []
        self.transcript: list[dict] = []  # provider-neutral, for logs and execution records
        self._shrunk: set[str] = set()

    def compact(self, shrink: Callable[[str, dict, str], str], keep_last: int = 2) -> int:
        """Shrink the results of tool calls made before the last `keep_last` model turns.

        Each old result becomes shrink(tool name, arguments, result text), once: a result already
        shrunk is left alone. Call it before `reply`. Returns how many characters it saved.

        Every later request sends less; the provider's prompt cache is broken from the first
        message that changed, so compacting in batches (keep_last=3 or 4) keeps more of it.
        """
        calls: dict[str, tuple[int, str, dict]] = {}
        turn = 0
        for entry in self.transcript:
            if entry["role"] == "assistant":
                turn += 1
                for c in entry["tool_calls"]:
                    calls[c["id"]] = (turn, c["name"], c["arguments"])
        cutoff = turn - keep_last
        saved = 0

        def new_text(cid: str, old: str) -> str | None:
            nonlocal saved
            if cid in self._shrunk or cid not in calls or calls[cid][0] > cutoff:
                return None
            _, name, arguments = calls[cid]
            text = str(shrink(name, arguments, old))
            self._shrunk.add(cid)
            saved += len(old) - len(text)
            return text

        for message in self.messages:
            if isinstance(message, dict) and message.get("role") == "tool":
                text = new_text(message["tool_call_id"], message["content"])
                if text is not None:
                    message["content"] = text
            elif isinstance(message, dict) and message.get("role") == "user" and isinstance(message["content"], list):
                for block in message["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        text = new_text(block["tool_use_id"], block["content"])
                        if text is not None:
                            block["content"] = text
        return saved

    def send(self, text: str) -> Turn:
        self.transcript.append({"role": "user", "text": text})
        if self.llm.provider.name == "anthropic":
            self.messages.append({"role": "user", "content": text})
        else:
            self.messages.append({"role": "user", "content": text})
        return self._step()

    def reply(self, results: dict[str, Any], errors: set[str] | frozenset[str] = frozenset()) -> Turn:
        """Send back the result of every tool call of the last turn (`errors`: the ids that failed)."""
        rendered = {cid: v if isinstance(v, str) else json.dumps(v, default=str) for cid, v in results.items()}
        for cid, content in rendered.items():
            self.transcript.append({"role": "tool", "id": cid, "content": content, "error": cid in errors})
        if self.llm.provider.name == "anthropic":
            blocks = [{"type": "tool_result", "tool_use_id": cid, "content": content, **({"is_error": True} if cid in errors else {})}
                      for cid, content in rendered.items()]  # fmt: skip
            self.messages.append({"role": "user", "content": blocks})
        else:
            for cid, content in rendered.items():
                self.messages.append({"role": "tool", "tool_call_id": cid, "content": content})
        return self._step()

    def _step(self) -> Turn:
        self.llm.usage.contexts.append(self.context_chars())
        turn = self._claude() if self.llm.provider.name == "anthropic" else self._openai_compatible()
        self.transcript.append(
            {"role": "assistant", "text": turn.text, "tool_calls": [c.__dict__ for c in turn.tool_calls]}
        )
        return turn

    def context_chars(self) -> int:
        """Characters of conversation the next request sends: every message so far, tool results included."""
        total = 0
        for m in self.messages:
            content = m.get("content") if isinstance(m, dict) else None
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        total += len(str(block.get("content") or block.get("text") or ""))
                    else:
                        total += len(getattr(block, "text", "") or "") + len(
                            json.dumps(getattr(block, "input", None) or {})
                        )
            if isinstance(m, dict) and m.get("tool_calls"):
                total += sum(len(c["function"]["arguments"]) for c in m["tool_calls"])
        return total

    def _claude(self) -> Turn:
        import anthropic

        llm = self.llm
        if llm._client is None:
            key = os.environ.get(llm.provider.key_env)
            llm._client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        request: dict[str, Any] = {
            "model": llm.model,
            "max_tokens": MAX_TOKENS,
            "messages": self.messages,
            "tools": [
                {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                for t in self.tools
            ],  # fmt: skip
            "cache_control": {"type": "ephemeral"},  # the tools and the growing history are read from cache
        }
        if self.system:
            request["system"] = self.system
        if llm.effort:
            request["output_config"] = {"effort": llm.effort}
        try:
            if llm.model in CLAUDE_FALLBACKS:
                response = llm._client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **request
                )
            else:
                response = llm._client.messages.create(**request)
        except anthropic.APIStatusError as e:
            raise LLMError(f"{llm}: the API answered {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"{llm}: can't reach the API ({e})") from e
        llm._count(response.usage.input_tokens, response.usage.output_tokens)
        if response.stop_reason == "refusal":
            raise LLMError(f"{llm}: the model declined this request")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"{llm}: the turn hit the {MAX_TOKENS}-token limit")
        self.messages.append({"role": "assistant", "content": response.content})
        text = "".join(b.text for b in response.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in response.content if b.type == "tool_use"]
        return Turn(text, calls)

    def _openai_compatible(self) -> Turn:
        import openai

        llm = self.llm
        if llm._client is None:
            key = os.environ.get(llm.provider.key_env)
            if not key:
                raise LLMError(f"Set {llm.provider.key_env} in .env for provider {llm.provider.name!r}")
            llm._client = openai.OpenAI(api_key=key, base_url=llm.provider.base_url)
        messages = ([{"role": "system", "content": self.system}] if self.system else []) + self.messages
        tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                   "parameters": t["parameters"]}} for t in self.tools]  # fmt: skip
        try:
            response = llm._client.chat.completions.create(model=llm.model, messages=messages, tools=tools)
        except openai.APIStatusError as e:
            raise LLMError(f"{llm}: the API answered {e.status_code}: {e.message}") from e
        except openai.APIConnectionError as e:
            raise LLMError(f"{llm}: can't reach {llm.provider.base_url or 'the API'} ({e})") from e
        if response.usage:
            llm._count(response.usage.prompt_tokens, response.usage.completion_tokens)
        message = response.choices[0].message
        calls = []
        for c in message.tool_calls or []:
            try:
                arguments = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {"_unparsed": c.function.arguments}
            calls.append(ToolCall(c.id, c.function.name, arguments))
        self.messages.append(
            {
                "role": "assistant",
                "content": message.content or "",
                **(
                    {
                        "tool_calls": [
                            {
                                "id": c.id,
                                "type": "function",
                                "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                            }
                            for c in calls
                        ]
                    }
                    if calls
                    else {}
                ),
            }  # fmt: skip
        )
        return Turn(message.content or "", calls)
