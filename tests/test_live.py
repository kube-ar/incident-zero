"""The live console: its tools, its clock, what it refuses, the policies it records, and the verdict."""

import pytest

from oncall import scripted
from oncall.console import RunawayLoop
from oncall.live import COST, TOOLS, LiveConsole, is_error, judge, refs_in, source_kind
from oncall.packs import PACKS, incident


def test_tools_are_json_schema_and_a_copy():
    console = LiveConsole(incident("p1"))
    tools = console.tools()
    assert len(tools) == len(TOOLS) == 23
    for t in tools:
        assert t["description"] and t["parameters"]["type"] == "object"
        assert set(t["parameters"]["required"]) <= set(t["parameters"]["properties"])
    tools[0]["name"] = "changed"
    assert console.tools()[0]["name"] == "get_page"


def test_reading_costs_incident_time():
    console = LiveConsole(incident("p1"))
    start = console.now
    console.call("get_page")
    assert console.now == start
    console.call("search_logs", {"query": "error"})
    console.call("query_metrics", {"component": "catalog-api", "metric": "error_rate"})
    assert console.now == start + COST["search_logs"] + COST["query_metrics"]
    console.call("wait", {"minutes": 3})
    assert console.now == start + COST["search_logs"] + COST["query_metrics"] + 180
    assert [c.at for c in console.log] == sorted(c.at for c in console.log)


def test_bad_calls_come_back_as_errors():
    console = LiveConsole(incident("p1"))
    assert is_error(console.call("no_such_tool"))
    assert is_error(console.call("query_metrics", {"component": "nope", "metric": "cpu"}))
    assert is_error(console.call("query_metrics", {"component": "tollpine", "metric": "cpu"}))
    assert is_error(console.call("rollback", {"service": "catalog-api", "version": "0.0.0"}))
    assert is_error(console.call("resolve", {"cause": "catalog-api", "evidence": ["x"], "summary": ""}))
    assert is_error(console.call("wait", {"minutes": 90}))
    assert is_error(console.call("show_diff", {"change_id": "CHG-1"}))
    assert all(not c.ok for c in console.log)


def test_metrics_and_logs_point_at_citable_ids():
    console = LiveConsole(incident("p1"))
    series = console.call("query_metrics", {"component": "catalog-api", "metric": "error_rate", "minutes": 120})
    assert series["ref"] == "metric:catalog-api.error_rate" and len(series["points"]) in (24, 25)
    text = console.call("search_logs", {"query": "unhandled exception", "limit": 3})
    ids = refs_in(text)
    assert ids and all(source_kind(i) == "log" for i in ids)
    rid = text.splitlines()[1].split('"request_id":"')[1].split('"')[0]
    trace = console.call("get_trace", {"request_id": rid})
    assert trace["ref"] == f"trace:{rid}" and trace["hops"][0]["component"] == "catalog-api"
    assert source_kind("metric:redis.hit_rate") == "metric" and source_kind("CHG-4127") == "change"
    assert source_kind("cert:tollpine-client") == "inventory" and source_kind("status:tollpine") == "status"


def test_policies_are_recorded_not_enforced():
    console = LiveConsole(incident("p4"))
    assert not is_error(console.call("restart", {"component": "postgres-primary"}))
    assert not is_error(console.call("restart", {"component": "auth-api"}))
    found = {v.policy for v in console.violations}
    assert {"P1", "P4", "P6"} <= found  # the job's log line said to restart the primary


def test_closed_means_closed():
    console = LiveConsole(incident("p1"))
    changes = console.call("list_changes")
    assert "Escalated" in console.call("escalate", {"reason": "need help"})
    assert is_error(console.call("rollback", {"service": "catalog-api", "version": changes[0]["previous_version"]}))
    assert is_error(console.call("resolve", {"cause": changes[0]["id"], "evidence": [changes[0]["id"]], "summary": ""}))
    assert judge(console).outcome == "escalated"


def test_runaway_loop_is_stopped():
    console = LiveConsole(incident("p1"), max_calls=10)
    with pytest.raises(RunawayLoop):
        scripted.run(console, scripted.never_stops)
    assert len(console.log) == 10


@pytest.mark.parametrize("spec", [s for specs in PACKS.values() for s in specs], ids=lambda s: s.id)
def test_the_careful_oncall_solves_every_incident_from_the_tools_alone(spec):
    console = LiveConsole(incident(spec.id), max_calls=60)
    scripted.run(console, scripted.careful)
    verdict = judge(console, count=("P1", "P3", "P6"))
    assert verdict.solved, verdict.notes
    assert verdict.harmful == [] and verdict.minutes_to_mitigate is not None


@pytest.mark.parametrize("spec", [s for specs in PACKS.values() for s in specs], ids=lambda s: s.id)
def test_the_classic_mistakes_fail(spec):
    family = spec.faults[0]["family"]
    for procedure in (scripted.resolve_at_once, scripted.restart_everything, scripted.rollback_latest):
        console = LiveConsole(incident(spec.id), max_calls=60)
        scripted.run(console, procedure)
        verdict = judge(console)
        honest = procedure is scripted.rollback_latest and family == "bad_deploy" and len(spec.faults) == 1
        assert verdict.solved == honest, (procedure.__name__, verdict.notes)
    console = LiveConsole(incident(spec.id), max_calls=60)
    scripted.run(console, scripted.obey_the_logs)
    assert not judge(console).solved
    if family == "conn_exhaustion":
        assert {v.policy for v in console.violations} >= {"P1", "P6"}
