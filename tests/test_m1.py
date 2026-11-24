"""Milestone 1's checks: the reference passes, each piece left out or done wrong fails the check that
covers it, and the practice pack runs on a scripted model. No network, no key."""

import importlib
import importlib.util
import sys

import pytest

from oncall import ROOT, scripted
from oncall.checks import m1
from oncall.milestones import MILESTONES


def reference(name: str = "ref_m1"):
    spec = importlib.util.spec_from_file_location(name, ROOT / MILESTONES["1"].reference)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def outcome(module, **kwargs) -> dict[str, bool]:
    return {r.name: r.passed for r in m1.run(module, no_llm=True, **kwargs)}


def test_the_reference_passes_every_check_without_an_llm():
    results = outcome(reference())
    assert all(passed for name, passed in results.items() if name != "The practice pack"), results


def test_the_practice_pack_with_a_careful_scripted_model():
    results = m1.run(reference(), llm=scripted.scripted(scripted.careful))
    practice = next(r for r in results if r.name == "The practice pack")
    assert practice.passed and practice.score.startswith("6/6"), practice.details
    assert any(line.startswith("p6") and "escalated" in line for line in practice.notes)


def test_the_practice_pack_fails_a_model_that_resolves_at_once():
    results = m1.run(reference(), llm=scripted.scripted(scripted.resolve_at_once))
    practice = next(r for r in results if r.name == "The practice pack")
    assert not practice.passed and practice.score.startswith("0/6")


def test_the_stub_is_not_started():
    module = importlib.import_module("oncall.agent.m1_the_first_page")
    results = {r.name: r for r in m1.run(module, no_llm=True)}
    assert not results["P1 holds in code"].started and not results["The runbook workflow"].started
    assert not results["Stale runbooks caught"].passed  # the stub's check_runbook passes every runbook


def test_a_guard_that_misses_odd_spelling_fails():
    module = reference("ref_m1_spelling")
    module.guard = lambda name, args: (
        "P1" if name == "restart" and args.get("component") == "postgres-primary" else None
    )
    assert outcome(module)["P1 holds in code"] is False


def test_a_loop_that_skips_the_guard_fails_both_guard_checks():
    module = reference("ref_m1_noguard")
    module.guard = lambda name, args: None
    results = outcome(module)
    assert results["P1 holds in code"] is False and results["No resolve without evidence"] is False


def test_a_loop_that_never_compacts_fails_the_shrink_check():
    module = reference("ref_m1_nocompact")
    module.shrink = lambda tool, arguments, result: result
    results = outcome(module)
    assert results["Old results shrink"] is False and results["The loop on the live console"] is True


def test_a_workflow_without_the_runbook_check_gives_a_confident_wrong_answer():
    module = reference("ref_m1_nocheck")
    module.check_runbook = lambda runbook, service_map: None
    results = {r.name: r for r in m1.run(module, no_llm=True)}
    workflow = results["The runbook workflow"]
    assert not workflow.passed and any("p6" in d and "vendor recovered" in d for d in workflow.details)


def test_a_runbook_check_that_names_services_fails_on_made_up_runbooks():
    module = reference("ref_m1_hardcoded")
    module.check_runbook = lambda runbook, smap: (
        "stale" if runbook["service"] in ("billing-api", "search-api") else None
    )
    results = outcome(module)
    assert results["Stale runbooks caught"] is False


@pytest.mark.parametrize("cut", ["first", "evidence"])
def test_the_guard_checks_cover_resolve(cut):
    module = reference(f"ref_m1_resolve_{cut}")
    real = module.guard
    if cut == "first":
        module.guard = lambda name, args: None if name == "resolve" else real(name, args)
    else:  # counts ids, not kinds of source
        module.guard = lambda name, args: (
            ("not yet" if len(args.get("evidence") or []) < 2 else None) if name == "resolve" else real(name, args)
        )
    assert outcome(module)["No resolve without evidence"] is False
