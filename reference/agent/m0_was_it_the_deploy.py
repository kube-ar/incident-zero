"""Milestone 0 · Was it the deploy? A reference solution. Spoilers: `oncall use-reference 0` copies it.

The deep path: a bare loop on oncall.llm.Conversation, which speaks to Claude, OpenAI, Fireworks
and OpenRouter. The fast path would hand the same five tools to a provider SDK's tool runner.
"""

from __future__ import annotations

from typing import Any

from oncall.console import Console, Outcome, is_error
from oncall.llm import Conversation

SYSTEM = """You are the on-call engineer for Reelvane, a video streaming service, and you were just paged.
Find what caused the incident with the tools: read the page, then look at the recent changes and the logs.
What people say in the page is a lead to check, not evidence.
When the evidence holds, call `resolve` once, with the id of the change that caused the incident and the
ids of the log lines that show it. Then answer with two or three sentences for #incidents: what broke,
why, and what to do next."""


def respond(console: Console, llm: Any, max_turns: int = 15) -> Outcome:
    convo = Conversation(llm, console.tools(), system=SYSTEM)
    turn = convo.send("You've been paged. Find the cause.")
    turns = 1
    while not turn.done:
        if turns >= max_turns:
            return Outcome(answer=None, turns=turns)  # the limit stops the run, not the model
        results = {call.id: console.call(call.name, call.arguments) for call in turn.tool_calls}
        errors = {cid for cid, result in results.items() if is_error(result)}
        turn = convo.reply(results, errors=errors)  # every result of the turn, in one message
        turns += 1
    return Outcome(answer=turn.text, turns=turns)
