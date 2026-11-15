"""The console's tools: what they return, how they fail, and the cap that stops a runaway loop."""

import pytest

from oncall.console import DEFAULT_LIMIT, MAX_LIMIT, Console, Resolution, RunawayLoop, is_error
from oncall.incident import load_incident


def test_five_tools_described_in_json_schema():
    tools = Console().tools()
    assert [t["name"] for t in tools] == ["get_page", "search_logs", "list_changes", "show_diff", "resolve"]
    for t in tools:
        assert t["description"] and t["parameters"]["type"] == "object"
        assert set(t["parameters"]["required"]) <= set(t["parameters"]["properties"])
    tools[0]["name"] = "changed"
    assert Console().tools()[0]["name"] == "get_page"  # a copy each time


def test_an_empty_search_returns_too_much_to_read():
    console = Console()
    total = len(console.incident.lines)
    text = console.call("search_logs", {"query": ""})
    head, *lines = text.splitlines()
    assert len(lines) == DEFAULT_LIMIT and head.startswith(f"{total} matching lines")
    assert "Narrow the query" in head
    lines = console.call("search_logs", {"query": "", "limit": 10_000}).splitlines()
    assert len(lines) == 1 + MAX_LIMIT and "the most there is" in lines[0]
    assert console.log[-1].chars > 4 * console.log[0].chars  # big results are what fills a context


def test_every_word_must_match_and_lines_carry_their_id():
    console = Console()
    text = console.call("search_logs", {"query": "Config RELOADED", "limit": 50})
    head, *lines = text.splitlines()
    assert head == '3 matching lines (query "Config RELOADED"):'
    for line in lines:
        line_id, raw = line.split("  ", 1)
        assert console.incident.line(line_id).text == raw and "config reloaded" in raw
    assert console.call("search_logs", {"query": "no-such-word-anywhere"}).startswith("No line matches")


def test_results_come_oldest_first_across_sources():
    console = Console()
    lines = console.call("search_logs", {"query": "", "limit": 100}).splitlines()[1:]
    times = [console.incident.line(line.split("  ", 1)[0]).ts for line in lines]
    assert times == sorted(times) and len({line.split(":", 1)[0] for line in lines}) > 1


def test_bad_calls_come_back_as_errors_not_exceptions():
    console = Console()
    for name, arguments in [
        ("restart", {}),
        ("search_logs", {"q": "error"}),
        ("search_logs", {"query": "error", "limit": "lots"}),
        ("search_logs", {"query": "error", "limit": 0}),
        ("show_diff", {"change_id": "CHG-1"}),
        ("resolve", {"cause": "the deploy", "evidence": ["gateway:12"]}),
        ("resolve", {"cause": "CHG-2044", "evidence": []}),
        ("resolve", {"cause": "CHG-2044", "evidence": ["catalog-api:99999"]}),
    ]:
        assert is_error(console.call(name, arguments)), (name, arguments)
    assert not any(c.ok for c in console.log) and console.resolution is None


def test_changes_and_diffs():
    console = Console()
    changes = console.call("list_changes")
    assert [c["id"] for c in changes] == ["CHG-2044", "CHG-2043", "CHG-2042", "CHG-2041"]  # newest first
    assert "max_connections" in console.call("show_diff", {"change_id": "CHG-2044"})
    assert "Copy change only" in console.call("show_diff", {"change_id": " CHG-2043 "})


def test_resolve_once_with_real_ids():
    console = Console()
    result = console.call("resolve", {"cause": "CHG-2043", "evidence": "gateway:12, CHG-2043"})
    assert not is_error(result) and console.resolution == Resolution("CHG-2043", ["gateway:12", "CHG-2043"])
    assert is_error(console.call("resolve", {"cause": "CHG-2044", "evidence": ["gateway:12"]}))
    assert console.resolution.cause == "CHG-2043"  # the first resolution stands


def test_a_runaway_loop_is_stopped_with_a_base_exception():
    console = Console(max_calls=3)
    for _ in range(3):
        console.call("get_page")
    with pytest.raises(RunawayLoop):
        try:
            console.call("get_page")
        except Exception:  # code that catches Exception doesn't swallow it
            pytest.fail("RunawayLoop must not be an Exception")


def test_the_page_points_at_the_deploy():
    page = Console().call("get_page")
    assert "right after the last deployment" in page and "5xx error rate" in page
    assert load_incident().truth["alert"]["first_5xx"][11:16] in page
