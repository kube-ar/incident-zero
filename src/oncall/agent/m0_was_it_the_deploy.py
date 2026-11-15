"""Milestone 0 · Was it the deploy?

Instructions: https://kube.ar/agents/project/incident-zero/was-it-the-deploy

You're on call for Reelvane, a video streaming service, and you've just been paged: the API is
returning 500s, and the support lead says it started right after the last deployment. The console
(oncall.console.Console) has five tools: get_page, search_logs, list_changes, show_diff, resolve.
Try them by hand first: `uv run oncall tools`, then `uv run oncall call get_page`.

Write the loop that lets a model use them. Send the conversation and the tool definitions, run
the tool calls the model asks for, send the results back, and repeat until the model answers
without asking for a tool, or until `max_turns` model turns have gone by.

Check your work with `uv run oncall check 0`. It runs your loop once with your LLM (see .env.example).
"""

from __future__ import annotations

from typing import Any

from oncall.console import Console, Outcome, is_error  # noqa: F401  (you'll need them)
from oncall.llm import Conversation  # noqa: F401  (the deep path; the fast path uses your provider's SDK)

# TODO: tell the model its job, which tools to trust, and what "done" means: call `resolve` with the
# change that caused the incident and the log lines that show it, then answer with a short summary.
SYSTEM = "You are on call for Reelvane, a video streaming service."


def respond(console: Console, llm: Any, max_turns: int = 15) -> Outcome:
    """Work the incident with `llm` (oncall.llm.get_llm()) and the console's tools; return an Outcome.

    1. Start a conversation with the tool definitions (console.tools()) and SYSTEM, and send a
       first message, like "You've been paged. Find the cause."
    2. While the model asks for tools: run each call with console.call(name, arguments), then send
       every result of that turn back together (tell the model which ones are errors: is_error).
    3. When the model answers without asking for a tool: return Outcome(answer=its text, turns=n).
    4. When `max_turns` model turns have gone by first: stop, and return Outcome(answer=None, turns=n).

    `turns` counts the calls to the model, the first one included. Don't call resolve for the
    model and don't answer for it: the check judges what the model did.
    """
    raise NotImplementedError
