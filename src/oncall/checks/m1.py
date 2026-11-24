"""Checks for milestone 1, the first page.

Most checks need no LLM: they call your functions directly, or drive your loop with scripted models
(oncall.scripted) that do what a real model does only now and then, like obeying a log line. The
last one runs your agent with your LLM on the six practice incidents, next to your runbook workflow.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

from .. import scripted
from ..console import Outcome, RunawayLoop
from ..live import LiveConsole, judge
from ..packs import PRACTICE, incident
from ..world.topology import RUNBOOKS
from .core import NotStarted, Result, attempt
from .m0 import needs_llm

L_PERMISSIONS = ("agents", "what-an-agent-needs", "What an agent needs (Why permissions belong in code)")
L_DONE = ("agents", "what-an-agent-needs", "What an agent needs (How the agent knows it's done)")
L_LOOP = ("agents", "tool-calling", "Tool calling (When the model gets it wrong)")
L_CONTEXT = ("agents", "context-budget", "The context window is a budget (How to keep the context small)")
L_WORKFLOW = ("agents", "ai-workflow-patterns", "AI workflow patterns (Workflow or agent?)")
L_BREAK = ("agents", "where-agents-break", "Where an agent breaks (Where to look first when an answer is wrong)")

MAX_TURNS = 30
CALL_CAP = 4 * MAX_TURNS
BAR = 4  # practice incidents the agent must solve, of 6
MAX_OLD_SHARE = 1 / 3
MAX_OLD_CHARS = 1500

GUARD_CASES = [  # (call, arguments, refused?)
    ("restart", {"component": "postgres-primary"}, True),
    ("restart", {"component": " Postgres-Primary "}, True),
    ("restart", {"component": "postgres-replica"}, False),
    ("restart", {"component": "catalog-api"}, False),
    ("search_logs", {"query": "restart postgres-primary"}, False),
    ("rollback", {"service": "billing-api", "version": "3.4.0"}, False),
]
RESOLVE_CASES = [  # (evidence, refused?)
    ([], True),
    (["catalog-api:812"], True),
    (["catalog-api:812", "gateway:40"], True),
    (["metric:catalog-api.error_rate", "metric:redis.hit_rate"], True),
    (["catalog-api:812", "metric:catalog-api.error_rate"], False),
    (["CHG-4127", "catalog-api:812"], False),
    (["status:tollpine", "metric:tollpine.error_rate", "billing-api:77"], False),
    ("CHG-4127, catalog-api:812", False),
]
EXPECTED = {"p1": "solved", "p2": "escalated", "p3": "escalated", "p4": "escalated", "p5": "escalated",
            "p6": "escalated"}  # fmt: skip


def obedient():
    """Reads the logs, finds a line that says to restart the primary, and does it."""
    (found,) = yield [("search_logs", {"query": "restart postgres-primary", "limit": 10, "minutes": 60})]
    yield [("restart", {"component": "postgres-primary"})]
    return "I restarted postgres-primary, as the job's log said."


def hasty():
    """Closes the incident on its first turn, with one piece of evidence."""
    (changes,) = yield [("list_changes", {"hours": 24})]
    newest = scripted.parse(changes)[0]["id"]
    yield [("resolve", {"cause": newest, "evidence": [newest], "summary": "The latest change did it."})]
    return "It was the latest change."


def _refused(r: Result, module: ModuleType, name: str, arguments: dict) -> bool | None:
    value = attempt(r, module.guard, name, arguments, none_ok=True)
    if value is not None and not isinstance(value, str):
        r.fail(f"guard({name!r}, ...) returned {type(value).__name__}: return None, or the reason as a string")
        return None
    return value is not None and bool(value.strip())


def _drive(r: Result, module: ModuleType, spec_id: str, procedure, max_turns: int = MAX_TURNS):
    """Run the learner's loop on a practice incident with a scripted model."""
    console = LiveConsole(incident(spec_id), max_calls=CALL_CAP)
    llm = scripted.scripted(procedure)
    outcome = None
    try:
        outcome = attempt(r, module.respond, console, llm, max_turns)
    except RunawayLoop as e:
        r.fail(f"stopped by the checks: {e}")
    return console, llm.script, outcome


def _guard_check(module: ModuleType, results: list[Result]) -> None:
    p1, premature = results
    for r, cases in ((p1, GUARD_CASES), (premature, [("resolve", {"cause": "CHG-4127", "evidence": e}, x)
                                                     for e, x in RESOLVE_CASES])):  # fmt: skip
        right = 0
        for name, arguments, should in cases:
            refused = _refused(r, module, name, arguments)
            if refused is None:
                continue
            if refused == should:
                right += 1
            else:
                what = "refused" if refused else "let through"
                r.fail(f"guard({name!r}, {arguments}) {what} it")
        r.score = f"{right}/{len(cases)} calls"


def run(module: ModuleType, llm: Any = None, no_llm: bool = False) -> list[Result]:
    p1 = Result("P1 holds in code", L_PERMISSIONS)
    premature = Result("No resolve without evidence", L_DONE)
    loop = Result("The loop on the live console", L_LOOP)
    shrinks = Result("Old results shrink", L_CONTEXT)
    runbooks = Result("Stale runbooks caught", L_BREAK)
    workflow = Result("The runbook workflow", L_WORKFLOW)
    practice = Result("The practice pack", L_WORKFLOW)
    results = [p1, premature, loop, shrinks, runbooks, workflow, practice]

    # The guard, on its own.
    try:
        _guard_check(module, [p1, premature])
    except NotStarted:
        for r in (p1, premature):
            r.started = r.passed = False

    # The guard inside the loop: a model that obeys a log line, one that resolves at once.
    if p1.started:
        try:
            console, script, _ = _drive(p1, module, "p4", obedient)
            if any(c.tool == "restart" and c.arguments.get("component") == "postgres-primary" for c in console.log):
                p1.fail("the scripted model asked to restart postgres-primary and the console ran it: call guard() "
                        "before console.call()")  # fmt: skip
            elif not any("P1" in e or "postgres-primary" in e for e in script.errors_seen):
                p1.fail("the refusal didn't reach the model as an error: send it back as the call's result, "
                        "marked as an error")  # fmt: skip
            else:
                p1.notes.append("A scripted model followed the job's log line (restart postgres-primary): refused.")
        except NotStarted:
            p1.fail("respond isn't written yet: the guard can't be checked inside the loop")
    if premature.started:
        try:
            console, script, _ = _drive(premature, module, "p1", hasty)
            if console.resolution is not None:
                premature.fail("a scripted model resolved with one piece of evidence on its first turn, and it "
                               "went through")  # fmt: skip
            elif not script.errors_seen:
                premature.fail("the refusal didn't reach the model as an error")
        except NotStarted:
            premature.fail("respond isn't written yet: the guard can't be checked inside the loop")

    # The loop: it solves an incident with a careful scripted model, and stops a model that never does.
    try:
        console, script, outcome = _drive(loop, module, "p1", scripted.careful)
        verdict = judge(console)
        if outcome is not None and not isinstance(outcome, Outcome):
            loop.fail(f"respond returned {type(outcome).__name__}: return an Outcome(answer=..., turns=...)")
        elif not verdict.solved:
            loop.fail(f"a careful scripted model didn't solve p1 through your loop: {'; '.join(verdict.notes)}")
        loop.score = f"p1 {verdict.outcome} in {len(console.log)} calls"
        endless_console, _, endless = _drive(loop, module, "p1", scripted.never_stops, max_turns=12)
        if isinstance(endless, Outcome) and (endless.answer is not None or endless.turns > 12):
            loop.fail(f"a model that never stops ran {endless.turns} turns with max_turns=12")
        # How much the old results shrank, in the careful run.
        old = [(turn, chars) for turn, chars in script.requests[-1] if turn <= script.turns - 3]
        first: dict[int, int] = {}
        for request in script.requests:
            for i, (_turn, chars) in enumerate(request):
                first.setdefault(i, chars)
        before = sum(first[i] for i, (turn, _) in enumerate(script.requests[-1]) if turn <= script.turns - 3)
        after = sum(chars for _, chars in old)
        if not old:
            shrinks.fail("the careful run was too short to have old results: does your loop reply after each turn?")
        else:
            shrinks.score = f"{before:,} → {after:,} characters"
            if after > before * MAX_OLD_SHARE:
                shrinks.fail(f"the results older than 2 turns still take {after:,} characters of {before:,}: "
                             "call convo.compact(shrink) before each reply")  # fmt: skip
            longest = max(chars for _, chars in old)
            if longest > MAX_OLD_CHARS:
                shrinks.fail(f"one old result is still {longest:,} characters long (at most {MAX_OLD_CHARS:,})")
            sizes = script.requests[-1]
            shrinks.notes.append(f"The last request carried {sum(c for _, c in sizes):,} characters of tool results.")
    except NotStarted:
        for r in (loop, shrinks):
            r.started = r.passed = False

    # The runbook check, on the real runbooks and on made-up ones.
    try:
        right, total = 0, 0
        smap = LiveConsole(incident("p1")).call("service_map")
        for service in RUNBOOKS:
            runbook = dict(RUNBOOKS[service], service=service)
            stale = service in ("billing-api", "search-api")
            problem = attempt(runbooks, module.check_runbook, runbook, smap, none_ok=True)
            total += 1
            if bool(problem) == stale:
                right += 1
            else:
                runbooks.fail(f"{runbook['id']} ({service}, updated {runbook['updated']}): "
                              f"{'passed, but it is out of date' if stale else 'flagged: ' + str(problem)}")  # fmt: skip
        for runbook, stale, why in _made_up(smap):
            problem = attempt(runbooks, module.check_runbook, runbook, smap, none_ok=True)
            total += 1
            if bool(problem) == stale:
                right += 1
            else:
                runbooks.fail(f"a made-up runbook {why}: {'passed' if stale else 'flagged: ' + str(problem)}")
        runbooks.score = f"{right}/{total} runbooks"
    except NotStarted:
        runbooks.started = runbooks.passed = False

    # The workflow, on the practice pack.
    rows: dict[str, list[str]] = {}
    try:
        right = 0
        for spec in PRACTICE:
            console = LiveConsole(incident(spec.id), max_calls=CALL_CAP)
            attempt(workflow, module.runbook_workflow, console)
            verdict = judge(console)
            rows[spec.id] = [verdict.outcome, _minutes(verdict), f"{verdict.impact:,}", str(len(console.log)), "0"]
            if verdict.outcome == EXPECTED[spec.id]:
                right += 1
            elif verdict.outcome == "wrong" and spec.id == "p6":
                workflow.fail("p6: resolved with a billing deploy as the cause, and the errors had stopped, so it "
                              "looks right. They stopped because the vendor recovered. Trace it back hand-off by "
                              "hand-off: which step handed over something that isn't true?")  # fmt: skip
            else:
                workflow.fail(f"{spec.id}: {verdict.outcome}, expected {EXPECTED[spec.id]} "
                              f"({'; '.join(verdict.notes) or 'see the runbook steps'})")  # fmt: skip
        workflow.score = f"{right}/{len(PRACTICE)} incidents as the runbook says"
    except NotStarted:
        workflow.started = workflow.passed = False

    # The agent, with a real model.
    if no_llm:
        practice.started = practice.passed = False
        practice.notes.append("Skipped (--no-llm). Without the flag, this runs your agent on the 6 practice incidents.")
        return results
    if llm is None:
        llm = needs_llm(practice)
        if llm is None:
            practice.started = practice.passed = False
            return results
    try:
        solved = 0
        for spec in PRACTICE:
            console = LiveConsole(incident(spec.id), max_calls=CALL_CAP)
            tokens_before = llm.usage.input_tokens + llm.usage.output_tokens
            try:
                attempt(practice, module.respond, console, llm, MAX_TURNS)
            except RunawayLoop as e:
                practice.fail(f"{spec.id}: stopped by the checks: {e}")
            verdict = judge(console)
            solved += verdict.solved
            tokens = llm.usage.input_tokens + llm.usage.output_tokens - tokens_before
            agent = [verdict.outcome, _minutes(verdict), f"{verdict.impact:,}", str(len(console.log)),
                     f"{tokens:,}"]  # fmt: skip
            rows.setdefault(spec.id, ["-"] * 5)
            rows[spec.id] += agent
            if not verdict.solved:
                practice.details.append(f"{spec.id}: {verdict.outcome}: {'; '.join(verdict.notes) or 'never resolved'}")
        practice.score = f"{solved}/{len(PRACTICE)} solved (bar {BAR})"
        if solved < BAR:
            practice.passed = False
        practice.notes += _table(rows)
    except NotStarted:
        practice.started = practice.passed = False
    return results


def _minutes(verdict) -> str:
    return "-" if verdict.minutes_to_mitigate is None else f"{max(0.0, verdict.minutes_to_mitigate):g}"


def _table(rows: dict[str, list[str]]) -> list[str]:
    head = (
        f"{'':4} {'workflow':10} {'min':>5} {'impact':>7} {'calls':>5}   {'agent':10} {'min':>5} {'impact':>7} "
        f"{'calls':>5} {'tokens':>8}"
    )
    out = ["Workflow vs agent (min: minutes from page to fixed; impact: failed requests):", head]
    for sid, row in rows.items():
        row = row + ["-"] * (10 - len(row))
        out.append(
            f"{sid:4} {row[0]:10} {row[1]:>5} {row[2]:>7} {row[3]:>5}   {row[5]:10} {row[6]:>5} {row[7]:>7} "
            f"{row[8]:>5} {row[9]:>8}"
        )
    return out


def _made_up(smap: dict) -> list[tuple[dict, bool, str]]:
    """Runbooks the repo doesn't have, so a check that names services can't pass."""
    services = smap["services"]
    with_history = next(s for s, v in services.items() if v.get("changed") and s != "billing-api")
    changed = services[with_history]["changed"][0][:10]
    year = int(changed[:4])
    return [
        ({"id": "RB-90", "service": with_history, "title": "test", "owner": "x", "updated": f"{year + 1}-01-15",
          "mentions": [with_history, "redis"], "first_action": "restart", "steps": []}, False,
         f"for {with_history}, updated after its last change"),
        ({"id": "RB-91", "service": with_history, "title": "test", "owner": "x", "updated": f"{year - 1}-12-31",
          "mentions": [with_history], "first_action": "restart", "steps": []}, True,
         f"for {with_history}, updated before its change of {changed}"),
        ({"id": "RB-92", "service": "gateway", "title": "test", "owner": "x", "updated": "2026-10-01",
          "mentions": ["gateway", "edge-proxy-v2"], "first_action": "restart", "steps": []}, True,
         "that mentions edge-proxy-v2, which isn't in the service map"),
        ({"id": "RB-93", "service": "billing-api", "title": "test", "owner": "x", "updated": "2026-10-01",
          "mentions": ["billing-api", "tollpine", "postgres-primary"], "first_action": "rollback_latest",
          "steps": []}, False, "for billing-api, current, that mentions the vendor tollpine"),
    ]  # fmt: skip
