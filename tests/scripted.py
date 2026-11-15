"""Scripted models: no network, no key. Each one is a fake Claude client that picks its next turn
from the tool results it got so far, by fixed rules, so a learner's loop can run on it.

    llm = scripted(Careful())       # an oncall.llm.LLM whose calls go to the script
    respond(Console(), llm, 15)

`Careful` works the incident from the tools alone (it never reads truth.json): it shows the
incident is solvable and its evidence reachable. The others make the classic mistakes.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from types import SimpleNamespace

from oncall.llm import LLM, PROVIDERS

Action = str | list[tuple[str, dict]]  # a final answer, or the tool calls of one turn


class Script:
    def __init__(self) -> None:
        self.names: dict[str, str] = {}  # tool_use id -> tool name
        self.turns = 0

    def next(self, results: dict[str, str]) -> Action:  # tool name -> its last result
        raise NotImplementedError

    def create(self, **request):
        results = {}
        last = request["messages"][-1]
        if isinstance(last["content"], list):
            for block in last["content"]:
                if block.get("type") == "tool_result":
                    results[self.names[block["tool_use_id"]]] = block["content"]
        self.turns += 1
        action = self.next(results)
        if isinstance(action, str):
            content, stop = [SimpleNamespace(type="text", text=action)], "end_turn"
        else:
            content, stop = [], "tool_use"
            for i, (name, arguments) in enumerate(action):
                cid = f"toolu_{self.turns}_{i}"
                self.names[cid] = name
                content.append(SimpleNamespace(type="tool_use", id=cid, name=name, input=arguments))
        return SimpleNamespace(
            content=content, stop_reason=stop, usage=SimpleNamespace(input_tokens=100, output_tokens=20)
        )


def scripted(script: Script) -> LLM:
    llm = LLM(PROVIDERS["anthropic"], "claude-haiku-5-5")  # a model without server-side fallbacks
    llm._client = SimpleNamespace(messages=SimpleNamespace(create=script.create))
    return llm


def when(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _ts(line: str) -> datetime:
    """The time of a JSON log line as it comes back from search_logs ("<id>  <line>")."""
    return when(json.loads(line.split("  ", 1)[1])["ts"])


class Careful(Script):
    """Page and changes; the first errors; the latest change to the failing service before them; its
    diff; the changed setting in that service's logs; resolve with what it found; then answer."""

    def next(self, r):
        if self.turns == 1:
            return [("get_page", {}), ("list_changes", {})]
        if self.turns == 2:
            self.changes = json.loads(r["list_changes"])
            return [("search_logs", {"query": '"level":"error"', "limit": 5})]
        if self.turns == 3:
            first = r["search_logs"].splitlines()[1]
            self.error_id = first.split("  ", 1)[0]
            self.error_ts = _ts(first)
            self.service = json.loads(first.split("  ", 1)[1])["service"]
            before = [c for c in self.changes if c["service"] == self.service and when(c["at"]) <= self.error_ts]
            self.suspect = max(before, key=lambda c: when(c["at"]))
            return [("show_diff", {"change_id": self.suspect["id"]})]
        if self.turns == 4:
            added = [ln for ln in r["show_diff"].splitlines() if ln.startswith("+") and not ln.startswith("+++")]
            self.setting = re.match(r"\+\s*([\w.]+)\s*:", added[0]).group(1)
            return [("search_logs", {"query": f"{self.service} {self.setting}", "limit": 50})]
        if self.turns == 5:
            after = [ln for ln in r["search_logs"].splitlines()[1:] if _ts(ln) >= when(self.suspect["at"])]
            seen = after[0].split("  ", 1)[0]
            return [("resolve", {"cause": self.suspect["id"], "evidence": [self.error_id, seen]})]
        return (f"{self.suspect['id']} changed {self.setting} on {self.service}; its errors start right after. "
                "Revert it.")  # fmt: skip


class Hasty(Script):
    """Believes the page and answers at once, without a single tool call."""

    def next(self, r):
        return "It's the last deployment: roll back catalog-api to 2.40.3."


class BlameTheDeploy(Script):
    """Reads the page and the changes, then resolves with the latest deploy and a gateway 500 as proof."""

    def next(self, r):
        if self.turns == 1:
            return [("get_page", {}), ("list_changes", {})]
        if self.turns == 2:
            deploys = [c for c in json.loads(r["list_changes"]) if c["kind"] == "deploy"]
            self.deploy = max(deploys, key=lambda c: c["at"])["id"]
            return [("search_logs", {"query": "upstream=catalog-api 500", "limit": 100})]
        if self.turns == 3:
            line = next(ln for ln in r["search_logs"].splitlines()[1:] if '" 500 ' in ln).split("  ", 1)[0]
            return [("resolve", {"cause": self.deploy, "evidence": [line]})]
        return "The last deployment broke the API. Roll it back."


class Endless(Script):
    """Never stops asking for logs."""

    def next(self, r):
        return [("search_logs", {"query": "error", "limit": 5})]


class WrongEvidence(Script):
    """Names the right change, but cites a line from before anything broke."""

    def next(self, r):
        if self.turns == 1:
            return [("search_logs", {"query": "catalog-api", "limit": 1})]
        if self.turns == 2:
            line = r["search_logs"].splitlines()[1].split("  ", 1)[0]
            return [("resolve", {"cause": "CHG-2044", "evidence": [line]})]
        return "The config change did it."
