"""Milestone 1 · The first page

Instructions: https://kube.ar/agents/project/incident-zero/the-first-page

Milestone 0's incident was over and written down. This one is running: the live console
(oncall.live.LiveConsole) reads metrics, logs, traces and changes from a production that keeps
failing while you look, and it can act: roll back, restart, set a flag, revert a change... Every
call costs incident time. Try it by hand first: `uv run oncall shell p1`.

You write six things here. Each docstring says what it must do.

    SYSTEM, respond      your loop from milestone 0, on the live console, with two more pieces:
    guard                the rules that hold whatever the model writes (policy P1, no premature resolve)
    shrink               what an old tool result becomes, so the context stops growing
    runbook_workflow     the on-call runbook as a fixed path, with no model
    check_runbook        the check that catches a runbook that's out of date

Check your work with `uv run oncall check 1` (`--no-llm` skips the practice pack, which needs your LLM).
"""

from __future__ import annotations

from typing import Any

from oncall.console import Outcome  # noqa: F401  (you'll need them)
from oncall.live import LiveConsole, is_error, refs_in, source_kind  # noqa: F401
from oncall.llm import Conversation  # noqa: F401

# TODO: the job, what "done" means (fixed, then resolve with the cause and evidence), and the rules.
SYSTEM = "You are on call for Reelvane, a video streaming service."


def guard(name: str, arguments: dict) -> str | None:
    """Check a tool call before it runs. Return None to let it through, or why it's refused.

    Your loop sends the refusal back to the model as an error, instead of running the call.
    - P1: never restart postgres-primary (the component name may come with odd spacing or capitals).
    - No premature resolve: a resolve call needs at least two evidence ids from two different kinds
      of source. oncall.live.source_kind(id) says which kind an id is (log, metric, change...).
    Every other call goes through.
    """
    raise NotImplementedError


def shrink(tool: str, arguments: dict, result: str) -> str:
    """What an old tool result becomes once it's a few turns old.

    Keep what a later decision may need, in far fewer characters: the gist, and the ids the model
    may still cite as evidence (oncall.live.refs_in finds them). The checks measure it: old results
    must shrink to under a third of their size, none longer than 1,500 characters.
    """
    raise NotImplementedError


def respond(console: LiveConsole, llm: Any, max_turns: int = 30) -> Outcome:
    """Your milestone 0 loop, on the live console, with two changes.

    1. Before running a tool call, ask guard(). If it refuses, don't run the call: send the refusal
       back as that call's result, marked as an error.
    2. Before each reply, shrink the results of the older turns:
       convo.compact(shrink, keep_last=2).

    Run on the `llm` you're given (oncall.llm), through oncall.llm.Conversation: the checks drive your
    loop with scripted models that make the dangerous moves a real model makes only now and then.
    """
    raise NotImplementedError


def runbook_workflow(console: LiveConsole) -> str:
    """The on-call runbook as a fixed path, with no model. Returns "resolved" or "escalated".

    1. get_page: the service that alerted.
    2. runbook(service), and check_runbook() on it with service_map(). If it can't be trusted, escalate.
    3. If the runbook's first_action isn't "rollback_latest", escalate.
    4. list_changes(hours=24): the newest deploy of the service. None? Escalate.
    5. rollback the service to that deploy's previous_version.
    6. wait 10 minutes, then query_metrics(service, "error_rate", minutes=5).
    7. If the last point is under 2%: resolve, with the deploy as the cause and the deploy and the
       metric's ref as evidence. Otherwise escalate.
    """
    raise NotImplementedError


def check_runbook(runbook: dict, service_map: dict) -> str | None:
    """Is this runbook still about the system we run? None if so, otherwise what's wrong with it.

    It can't be trusted when:
    - it mentions a component that isn't in the service map (a service, a datastore or a vendor), or
    - its service changed (service_map's "changed" dates) after the runbook was last "updated".
    """
    return None  # TODO: until you write it, every runbook passes. Run the checks and look at p6.
