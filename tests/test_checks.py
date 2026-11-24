"""Milestone 0's check: the reference passes with a careful model, each classic mistake fails, and
nothing runs without an LLM. Scripted models only: no network, no key."""

import importlib
import importlib.util
import os
import sys
import types

import pytest
from scripted import BlameTheDeploy, Careful, Endless, Hasty, WrongEvidence, scripted

from oncall import ROOT
from oncall import llm as llm_mod
from oncall.checks import m0
from oncall.console import Outcome, is_error
from oncall.llm import Conversation
from oncall.milestones import MILESTONES

ALL = {
    "The loop stops on its own",
    "It looks before it answers",
    "The right cause",
    "Evidence from the logs",
}


def load(path):
    spec = importlib.util.spec_from_file_location(f"ref_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def reference():
    return load(ROOT / MILESTONES["0"].reference)


def failing(module, script) -> set[str]:
    return {r.name for r in m0.run(module, scripted(script)) if not r.passed}


def details(module, script) -> str:
    return "\n".join(line for r in m0.run(module, scripted(script)) for line in [*r.details, *r.notes])


def test_reference_with_a_careful_model_passes():
    results = m0.run(reference(), scripted(Careful()))
    assert all(r.passed and r.started for r in results), [(r.name, r.details) for r in results]


def test_answering_without_looking_fails():
    assert failing(reference(), Hasty()) == ALL - {"The loop stops on its own"}


def test_blaming_the_last_deploy_fails():
    assert failing(reference(), BlameTheDeploy()) == {"The right cause", "Evidence from the logs"}
    assert "CHG-2043 is the last deployment" in details(reference(), BlameTheDeploy())


def test_a_model_that_never_stops_hits_the_turn_limit():
    assert failing(reference(), Endless()) == {
        "The loop stops on its own",
        "The right cause",
        "Evidence from the logs",
    }
    assert "the turn limit stopped the run (15 turns)" in details(reference(), Endless())


def test_evidence_that_shows_nothing_fails():
    assert failing(reference(), WrongEvidence()) == {"Evidence from the logs"}


def no_turn_limit(console, llm, max_turns=15):
    convo = Conversation(llm, console.tools())
    turn = convo.send("You've been paged.")
    turns = 1
    while not turn.done:  # no limit: only the console's cap stops this
        results = {c.id: console.call(c.name, c.arguments) for c in turn.tool_calls}
        turn = convo.reply(results, errors={cid for cid, r in results.items() if is_error(r)})
        turns += 1
    return Outcome(turn.text, turns)


def test_a_loop_without_a_turn_limit_is_stopped_and_fails():
    module = types.SimpleNamespace(respond=no_turn_limit)
    assert "The loop stops on its own" in failing(module, Endless())
    assert "does your loop stop after max_turns" in details(module, Endless())
    # with a model that stops, the same loop is fine: the check judges the run, not the code
    assert failing(module, Careful()) == set()


def test_a_forgotten_return_fails():
    ref = reference()

    def forgot(console, llm, max_turns=15):
        ref.respond(console, llm, max_turns)

    module = types.SimpleNamespace(respond=forgot)
    assert "The loop stops on its own" in failing(module, Careful())
    assert "returned None" in details(module, Careful())


def test_a_loop_that_hides_the_limit_fails():
    ref = reference()

    def pretends(console, llm, max_turns=15):
        out = ref.respond(console, llm, max_turns)
        return Outcome(out.answer or "", out.turns)  # an empty answer is not an answer

    assert "The loop stops on its own" in failing(types.SimpleNamespace(respond=pretends), Endless())


def test_nothing_runs_without_an_llm(monkeypatch):
    for k in ("ONCALL_LLM_PROVIDER", "ONCALL_LLM_MODEL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(llm_mod, "_load_env", lambda: None)
    results = m0.run(reference())
    assert all(not r.started and not r.passed for r in results)
    assert "Needs an LLM" in results[0].notes[0]


UPSTREAM = os.environ.get("GITHUB_REPOSITORY", "") == "kube-ar/incident-zero" or os.environ.get("ONCALL_UPSTREAM")


# Checks a stub starts on purpose: milestone 1's check_runbook passes every runbook until it's written, so
# the learner can watch the workflow give a confident wrong answer on p6 first.
STARTED_BY_DESIGN = {"1": {"Stale runbooks caught"}}


@pytest.mark.skipif(not UPSTREAM, reason="your copy has your code in the starter files, not the stubs")
@pytest.mark.parametrize("mid", sorted(MILESTONES))
def test_starter_is_not_started(mid):
    m = MILESTONES[mid]
    results = importlib.import_module(m.checks).run(load(ROOT / m.file), scripted(Careful()))
    expected = STARTED_BY_DESIGN.get(mid, set())
    assert {r.name for r in results if r.started} == expected
