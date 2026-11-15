"""The on-call console: the tools an agent gets for one incident, and a record of every call.

    console = Console()                     # milestone 0's incident
    console.tools()                         # tool definitions (JSON Schema), for any provider
    console.call("search_logs", {"query": "error", "limit": 5})
    console.log                             # every call, in order
    console.resolution                      # set once `resolve` succeeds

Tools never raise: a bad call comes back as {"error": "..."} (is_error(result) is true), so the
model can read what went wrong and try again. Milestone 0's console only reads; actions that
change the system (rollback, restart...) come in later milestones.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .incident import Incident, load_incident

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


class RunawayLoop(BaseException):
    """Raised by the console when a run makes far more tool calls than its turn limit allows.

    A BaseException, not an Exception, so a loop or an SDK that catches Exception doesn't swallow
    it: it exists to stop a loop without a working turn limit before it spends real money.
    """


@dataclass
class Call:
    n: int
    tool: str
    arguments: dict
    ok: bool
    chars: int  # size of the result the model gets back, in characters


@dataclass
class Resolution:
    cause: str
    evidence: list[str]


@dataclass
class Outcome:
    """How a run ended: what your loop returns."""

    answer: str | None  # the model's final text; None when the turn limit stopped the run first
    turns: int  # calls to the model, the first one included


def is_error(result: Any) -> bool:
    """Whether a tool result is an error ({"error": ...})."""
    return isinstance(result, dict) and "error" in result


TOOLS = [
    {
        "name": "get_page",
        "description": "The page that woke you up: the alert that fired and what the person who raised it said. "
        "Start here.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "name": "search_logs",
        "description": "Search the logs, oldest line first: the gateway's nginx access log and the JSON logs of "
        "catalog-api and playback-api. Every word of `query` must appear in a line (case doesn't matter), and "
        "an empty query matches every line. Each line comes with its id, like gateway:12, to cite as "
        "evidence. Returns at most `limit` lines: narrow the query rather than raising the limit (a service "
        "name, a status code, a request id, a word from a message, or a time like 14:05).",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words that must all appear in a line; '' for every line."},
                "limit": {
                    "type": "integer",
                    "description": f"Most lines to return (default {DEFAULT_LIMIT}, at most {MAX_LIMIT}).",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_changes",
        "description": "Today's changes to production, newest first: deploys (service, version, commit message) "
        "and config changes. show_diff shows what one of them changed.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "name": "show_diff",
        "description": "What one change changed: the commit and its diff for a deploy, the file diff for a config "
        "change.",
        "parameters": {
            "type": "object",
            "properties": {"change_id": {"type": "string", "description": "A change id from list_changes."}},
            "required": ["change_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "resolve",
        "description": "Close the investigation, once the evidence holds. `cause`: the id of the change that "
        "caused the incident (from list_changes). `evidence`: ids of the log lines (like gateway:12) and "
        "changes that show it. You can resolve only once; then answer with a short summary for #incidents.",
        "parameters": {
            "type": "object",
            "properties": {
                "cause": {"type": "string", "description": "The id of the change that caused the incident."},
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ids of the log lines and changes that show it.",
                },
            },
            "required": ["cause", "evidence"],
            "additionalProperties": False,
        },
    },
]


@dataclass
class Console:
    incident: Incident = field(default_factory=load_incident)
    max_calls: int | None = None  # the checks set it: past it, the console raises RunawayLoop
    log: list[Call] = field(default_factory=list)
    resolution: Resolution | None = None

    def tools(self) -> list[dict]:
        """The tool definitions: name, description and parameters (a JSON Schema object)."""
        return json.loads(json.dumps(TOOLS))  # a copy: changing it can't change the console

    def call(self, name: str, arguments: dict | None = None) -> Any:
        """Run a tool by name. Errors come back as {"error": ...}, never as exceptions."""
        if self.max_calls is not None and len(self.log) >= self.max_calls:
            raise RunawayLoop(
                f"{len(self.log)} tool calls and still going: does your loop stop after max_turns model turns?"
            )
        arguments = dict(arguments or {})
        names = [t["name"] for t in TOOLS]
        if name not in names:
            return self._record(name, arguments, {"error": f"no tool {name!r}: the tools are {', '.join(names)}"})
        try:
            result = getattr(self, f"_{name}")(**arguments)
        except TypeError as e:
            result = {"error": f"bad arguments for {name}: {e}"}
        return self._record(name, arguments, result)

    def _record(self, name: str, arguments: dict, result: Any) -> Any:
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        self.log.append(Call(len(self.log) + 1, name, arguments, not is_error(result), len(text)))
        return result

    # ── The tools ─────────────────────────────────────────────────────────────

    def _get_page(self) -> str:
        return self.incident.page

    def _search_logs(self, query: str = "", limit: int = DEFAULT_LIMIT) -> Any:
        if not isinstance(query, str):
            return {"error": "query must be a string of words, like 'catalog-api error'"}
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return {"error": f"limit must be a number from 1 to {MAX_LIMIT}"}
        if limit < 1:
            return {"error": f"limit must be a number from 1 to {MAX_LIMIT}"}
        capped = limit > MAX_LIMIT
        limit = min(limit, MAX_LIMIT)
        words = query.lower().split()
        hits = [line for line in self.incident.lines if all(w in f"{line.id} {line.text}".lower() for w in words)]
        shown = hits[:limit]
        about = f'query "{query}"' if query else "empty query"
        if not hits:
            return f"No line matches ({about}). Try fewer or shorter words."
        head = f"{len(hits)} matching lines ({about})"
        if len(hits) > len(shown):
            head += f", showing the oldest {len(shown)} (limit {limit}"
            head += ", the most there is)" if capped else ")"
            head += ". Narrow the query to see the others: a service, a status code, a request id, a time like 14:05."
        else:
            head += ":"
        return "\n".join([head, *(f"{line.id}  {line.text}" for line in shown)])

    def _list_changes(self) -> list[dict]:
        return self.incident.changes

    def _show_diff(self, change_id: str) -> Any:
        diff = self.incident.diffs.get(str(change_id).strip())
        if diff is None:
            ids = ", ".join(c["id"] for c in self.incident.changes)
            return {"error": f"no change {change_id!r}: the changes are {ids}"}
        return diff

    def _resolve(self, cause: str, evidence: list[str] | str) -> Any:
        if self.resolution is not None:
            return {"error": "already resolved: write your summary for #incidents"}
        cause = str(cause).strip()
        if self.incident.change(cause) is None:
            ids = ", ".join(c["id"] for c in self.incident.changes)
            return {"error": f"unknown change {cause!r}: cause must be one change id from list_changes ({ids})"}
        if isinstance(evidence, str):
            evidence = [e for e in re.split(r"[\s,]+", evidence) if e]
        if not isinstance(evidence, list) or not evidence or not all(isinstance(e, str) for e in evidence):
            return {"error": "evidence must be a list of log line ids (like gateway:12) and change ids"}
        evidence = [e.strip() for e in evidence]
        unknown = [e for e in evidence if self.incident.line(e) is None and self.incident.change(e) is None]
        if unknown:
            return {
                "error": f"no such ids: {', '.join(unknown)}. Log line ids look like gateway:12 (search_logs "
                "shows them); change ids like CHG-2041 (list_changes)."
            }
        self.resolution = Resolution(cause, evidence)
        return (
            f"Resolved: cause {cause}, evidence {', '.join(evidence)}. Now answer with two or three sentences "
            "for #incidents: what broke, why, and what to do next."
        )
