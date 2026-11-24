"""The simulator: deterministic, healthy before the fault, paged soon after, and the past never changes."""

import math

import pytest

from oncall.live import LiveConsole
from oncall.packs import ALL, incident
from oncall.world import Spec, World
from oncall.world.actions import ActionError
from oncall.world.faults import FAMILIES


def test_same_seed_same_incident():
    a, b = incident("p2"), incident("p2")
    assert a.page == b.page
    t = a.page["fired_at"] + 300
    assert [(x.id, x.text) for x in a.lines_until(t)] == [(x.id, x.text) for x in b.lines_until(t)]
    assert a.state(math.floor(t / 60)).metrics == b.state(math.floor(t / 60)).metrics


@pytest.mark.parametrize("incident_id", sorted(ALL))
def test_healthy_before_paged_soon_after(incident_id):
    w = incident(incident_id)
    start = min(f.start for f in w.faults)
    assert w.healthy(math.floor(start / 60) - 20) or w.faults[0].family == "memory_leak"
    assert 0 < w.page["fired_at"] - start < 6 * 60
    assert w.page["service"] in w.topo.services()
    assert not w.healthy(math.floor(w.page["fired_at"] / 60) - 1) or w.faults[0].family == "memory_leak"


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_every_family_leaves_evidence_in_the_logs_or_the_inventory(family):
    w = World(Spec("t", 9, [{"family": family}]))
    lines = w.lines_until(w.page["fired_at"] + 300)
    tagged = [x for x in lines if x.tags]
    fault = w.faults[0]
    assert tagged or any(ref.startswith(("cert:", "node:", "status:")) for ref in fault.support)
    assert fault.causes and fault.story()
    truth = fault.truth()
    assert truth["family"] == family and truth["causes"] == fault.causes


def test_what_was_read_never_changes():
    w = incident("p1")
    console = LiveConsole(w)
    before = console.call("search_logs", {"query": "catalog-api error", "limit": 100})
    page = console.call("list_changes", {"hours": 6})
    deploy = next(c for c in page if c["kind"] == "deploy" and c["target"] == "catalog-api")
    console.call("rollback", {"service": "catalog-api", "version": deploy["previous_version"]})
    console.call("wait", {"minutes": 10})
    after = [x for x in w.lines_until(console.now) if "catalog-api error" in x.text]
    for line in before.splitlines()[1:]:
        line_id, text = line.split("  ", 1)
        assert w.line(line_id).text == text
    assert after is not None


def test_a_fix_stops_the_errors_and_a_useless_action_does_not():
    w = incident("p2")  # a config change on playback-api, with an innocent deploy just before
    console = LiveConsole(w)
    deploy = next(c for c in console.call("list_changes") if c["kind"] == "deploy")
    console.call("rollback", {"service": "playback-api", "version": deploy["previous_version"]})
    console.call("wait", {"minutes": 6})
    assert "playback-api" in " ".join(console.call("wait", {"minutes": 1})["firing_now"])
    config = next(c for c in console.call("list_changes") if c["kind"] == "config" and c["target"] == "playback-api")
    console.call("revert_config", {"change_id": config["id"]})
    console.call("wait", {"minutes": 4})
    assert console.call("wait", {"minutes": 1})["firing_now"] == ["nothing: every service is healthy"]


def test_actions_are_checked():
    w = incident("p1")
    with pytest.raises(ActionError, match="never ran"):
        w.act(1, w.page["fired_at"] + 60, "rollback", {"service": "catalog-api", "version": "0.0.1"})
    with pytest.raises(ActionError, match="no flag"):
        w.act(1, w.page["fired_at"] + 60, "set_flag", {"flag": "nope", "value": "on"})
    with pytest.raises(ActionError, match="percentage"):
        w.act(1, w.page["fired_at"] + 60, "set_flag", {"flag": "recs.new_ranker", "value": "lots"})
    assert w.acts == []
