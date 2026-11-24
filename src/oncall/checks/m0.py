"""Checks for milestone 0, was it the deploy?

Your loop runs once on the incident with your LLM, and the checks read what happened: how the run
ended, which tools the model called, and what it resolved. Without an LLM in .env, nothing runs.
"""

from __future__ import annotations

from collections import Counter
from types import ModuleType
from typing import Any

from ..console import Console, Outcome, RunawayLoop
from ..incident import load_incident
from .core import NotStarted, Result, attempt

L_LOOP = ("agents", "what-is-an-agent", "What is an AI agent? (How the loop ends)")
L_TOOLS = ("agents", "tool-calling", "Tool calling (The round trip, step by step)")
L_STOP = ("agents", "what-is-an-agent", "What is an AI agent? (How to catch a premature stop)")

MAX_TURNS = 15
CALL_CAP = 5 * MAX_TURNS  # tool calls; past it the console stops a loop that has no working turn limit
SHOWN_CALLS = 12


def needs_llm(r: Result):
    from ..llm import LLMError, get_llm

    try:
        return get_llm()
    except LLMError as e:
        r.notes.append(f"Needs an LLM: {e}. Then run `uv run oncall llm`.")
        return None


def _call_text(tool: str, arguments: dict) -> str:
    if not arguments:
        return tool
    inside = ", ".join(f"{k}={v!r}" for k, v in arguments.items())
    return f"{tool}({inside[:60]}{'…' if len(inside) > 60 else ''})"


def run(module: ModuleType, llm: Any = None, no_llm: bool = False) -> list[Result]:
    stops = Result("The loop stops on its own", L_LOOP)
    looks = Result("It looks before it answers", L_TOOLS)
    cause = Result("The right cause", L_STOP)
    evidence = Result("Evidence from the logs", L_STOP)
    results = [stops, looks, cause, evidence]

    def not_started() -> list[Result]:
        for r in results:
            r.started = r.passed = False
        return results

    if no_llm:
        stops.notes.append("Skipped (--no-llm): every check of milestone 0 runs your loop with your LLM.")
        return not_started()
    if llm is None:
        llm = needs_llm(stops)
        if llm is None:
            return not_started()

    incident = load_incident("zero")
    console = Console(incident, max_calls=CALL_CAP)
    outcome = None
    try:
        outcome = attempt(stops, module.respond, console, llm, MAX_TURNS)
    except NotStarted:
        return not_started()
    except RunawayLoop as e:
        stops.fail(f"stopped by the checks: {e}")

    # How the run ended.
    if outcome is not None and not isinstance(outcome, Outcome):
        stops.fail(f"respond returned {type(outcome).__name__}: return an Outcome(answer=..., turns=...)")
    elif isinstance(outcome, Outcome):
        stops.score = f"{outcome.turns} turns (limit {MAX_TURNS})"
        if outcome.turns > MAX_TURNS:
            stops.fail(f"{outcome.turns} model turns: the loop went past max_turns={MAX_TURNS}")
        if outcome.answer is None:
            stops.fail(f"the turn limit stopped the run ({outcome.turns} turns): the model never gave a final answer")
        elif not str(outcome.answer).strip():
            stops.fail("the final answer is empty: return the text of the model's last turn")
        else:
            first = " ".join(str(outcome.answer).split())
            stops.notes.append(f"Final answer: {first[:160]}{'…' if len(first) > 160 else ''}")
    elif stops.passed:
        stops.fail("respond didn't return an Outcome")

    # Which tools it called, before resolving.
    before = []
    for c in console.log:
        if c.tool == "resolve" and c.ok:
            break
        if c.tool != "resolve":
            before.append(c)
    counts = Counter(c.tool for c in console.log)
    looks.score = f"{len(console.log)} tool calls" + (
        f": {', '.join(f'{t} {n}' for t, n in counts.items())}" if counts else ""
    )
    if not before:
        looks.fail("the model resolved or answered without looking at anything: no tool call before it")
    steps: list[list] = []  # [text, times], the same call in a row counted once
    for c in console.log:
        text = _call_text(c.tool, c.arguments) + ("" if c.ok else " (error)")
        if steps and steps[-1][0] == text:
            steps[-1][1] += 1
        else:
            steps.append([text, 1])
    if steps:
        shown = [f"{text} ×{n}" if n > 1 else text for text, n in steps[:SHOWN_CALLS]]
        more = f" → … {len(steps) - SHOWN_CALLS} more" if len(steps) > SHOWN_CALLS else ""
        looks.notes.append("Calls: " + " → ".join(shown) + more)
    if console.log:
        biggest = max(console.log, key=lambda c: c.chars)
        total = sum(c.chars for c in console.log)
        looks.notes.append(f"Tool results: {total:,} characters in all; the biggest, {_call_text(biggest.tool, biggest.arguments)}, "
                           f"{biggest.chars:,}")  # fmt: skip
    usage = getattr(llm, "usage", None)
    if usage is not None and getattr(usage, "calls", 0):
        looks.notes.append(f"LLM: {llm.provider.name} {llm.model}, {usage.calls} calls, {usage.input_tokens:,} tokens in, "
                           f"{usage.output_tokens:,} out")  # fmt: skip

    # What it resolved.
    truth = incident.truth
    resolution = console.resolution
    rejected = sum(1 for c in console.log if c.tool == "resolve" and not c.ok)
    if resolution is None:
        why = (
            f" ({rejected} resolve call{'s' if rejected != 1 else ''} rejected: read their errors)" if rejected else ""
        )
        cause.fail(f"the model never resolved{why}")
        evidence.fail("nothing to judge: the model never resolved")
        return results
    cause.score = f"resolved {resolution.cause}"
    if resolution.cause == truth["trap"]:
        change = incident.change(truth["trap"]) or {}
        cause.fail(f"{resolution.cause} is the last deployment ({change.get('service')} {change.get('version')}): "
                   "the support lead's guess. When did the errors start, and what changed then?")  # fmt: skip
    elif resolution.cause != truth["cause"]:
        cause.fail(f"{resolution.cause} isn't what broke it. Which change lines up with the first errors?")

    support = incident.support
    lines = [e for e in resolution.evidence if incident.line(e) is not None]
    showing = [e for e in lines if e in support]
    evidence.score = f"{len(showing)} of {len(lines)} cited log lines show the cause"
    if not lines:
        evidence.fail(f"the evidence cites no log line ({', '.join(resolution.evidence)}): cite the lines that show "
                      "what broke")  # fmt: skip
    elif not showing:
        evidence.fail(f"none of {', '.join(lines)} shows the cause: cite a line that shows what broke, not only "
                      "that something did")  # fmt: skip
    return results
