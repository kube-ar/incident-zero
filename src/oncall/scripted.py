"""Scripted on-calls: no model, no key. The checks and the tests use them to drive a console, or a
learner's loop, through moves a real model makes only now and then.

An on-call is a procedure: a generator that yields the tool calls of one turn, gets their results
back (in the same order), and finally returns its answer. The same procedure runs two ways:

    run(console, careful)              # calls the console's tools directly
    llm = scripted(careful)            # a fake Claude that plays it inside any loop built on oncall.llm
    respond(console, llm, 30)

`careful` works an incident from what the tools return (it never reads the truth): it proves every
incident is solvable and its evidence reachable. The others make the classic mistakes.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Generator
from types import SimpleNamespace
from typing import Any

from .llm import LLM, PROVIDERS

Calls = list[tuple[str, dict]]
Procedure = Callable[[], Generator[Calls, list[Any], str]]


def parse(result: Any) -> Any:
    """A tool result as data: JSON text becomes a dict or a list; other text stays text."""
    if isinstance(result, str) and result[:1] in "[{":
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return result
    return result


def failed(result: Any) -> bool:
    result = parse(result)
    return isinstance(result, dict) and "error" in result


def log_lines(text: Any) -> list[tuple[str, str]]:
    """(id, line) for each line of a search_logs result."""
    if not isinstance(text, str):
        return []
    out = []
    for row in text.splitlines()[1:]:
        line_id, _, line = row.partition("  ")
        out.append((line_id, line))
    return out


def run(console, procedure: Procedure) -> str:
    """Drive a console with a procedure, no model in between."""
    gen = procedure()
    results = None
    try:
        while True:
            calls = gen.send(results)
            results = [console.call(name, args) for name, args in calls]
    except StopIteration as stop:
        return stop.value


# ── A fake model ─────────────────────────────────────────────────────────────────


class Script:
    """A fake Claude client: each request gets the next turn of a procedure.

    It also keeps what every request carried (`requests`), so a check can measure the context a
    loop sends: the size of each tool result, and the turn it came from.
    """

    def __init__(self, procedure: Procedure):
        self.procedure = procedure
        self.gen = procedure()
        self.started = False
        self.order: list[str] = []  # tool_use ids of the last turn, in order
        self.turns = 0
        self.requests: list[list[tuple[int, int]]] = []  # per request: (turn of the call, chars) of each tool result
        self.turn_of: dict[str, int] = {}
        self.answer: str | None = None
        self.errors_seen: list[str] = []

    def create(self, **request):
        if len(request["messages"]) == 1 and self.started:  # a new conversation: start the procedure over
            self.gen, self.started, self.answer, self.order = self.procedure(), False, None, []
            self.requests, self.turn_of, self.turns = [], {}, 0
        self.turns += 1
        sizes = []
        for message in request["messages"]:
            if message["role"] == "user" and isinstance(message["content"], list):
                for block in message["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        sizes.append((self.turn_of.get(block["tool_use_id"], 0), len(str(block["content"]))))
        self.requests.append(sizes)
        results: list[Any] | None = None
        last = request["messages"][-1]
        if self.started:
            by_id = {}
            if isinstance(last["content"], list):
                for block in last["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        by_id[block["tool_use_id"]] = block["content"]
                        if block.get("is_error"):
                            self.errors_seen.append(str(block["content"]))
            results = [by_id.get(cid, {"error": "no result came back for this call"}) for cid in self.order]
        self.started = True
        if self.answer is not None:
            calls: Calls | str = self.answer
        else:
            try:
                calls = self.gen.send(results)
            except StopIteration as stop:
                self.answer = calls = stop.value or "Done."
        if isinstance(calls, str):
            content, stop_reason = [SimpleNamespace(type="text", text=calls)], "end_turn"
        else:
            content, stop_reason, self.order = [], "tool_use", []
            for i, (name, arguments) in enumerate(calls):
                cid = f"toolu_{self.turns:03d}_{i}"
                self.order.append(cid)
                self.turn_of[cid] = self.turns
                content.append(SimpleNamespace(type="tool_use", id=cid, name=name, input=arguments))
        usage = SimpleNamespace(input_tokens=sum(c for _, c in sizes) // 4 + 400, output_tokens=60)
        return SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage)


def scripted(procedure: Procedure) -> LLM:
    """An oncall.llm.LLM whose calls go to a scripted procedure instead of a provider."""
    llm = LLM(PROVIDERS["anthropic"], "scripted")
    llm.script = Script(procedure)  # type: ignore[attr-defined]
    llm._client = SimpleNamespace(messages=SimpleNamespace(create=llm.script.create))  # type: ignore[attr-defined]
    return llm


# ── The careful on-call ─────────────────────────────────────────────────────────

SIGNATURES = [  # (what an error says, the fault family it points to)
    ("OOMKilled", "memory_leak"),
    ("unhandled exception", "bad_deploy"),
    ("timeout acquiring a database connection", "config_change"),
    ("x509", "expired_cert"),
    ("too many clients", "conn_exhaustion"),
    ("No space left on device", "disk_full"),
    ("statement timeout", "cache_stampede"),
    ("tollpine: 503", "vendor_outage"),
    ("es_rejected_execution", "retry_storm"),
    ("ranker v2", "flag_rollout"),
    ("connection refused", "dns_change"),
    ("cpu throttled", "noisy_neighbor"),
]


def _newest(changes: list[dict], **match) -> dict | None:
    for c in changes:  # newest first
        if all((v(c.get(k)) if callable(v) else c.get(k) == v) for k, v in match.items()):
            return c
    return None


def _minutes(hm: str) -> int:
    """'14:05' or '14:05:30' as minutes from midnight."""
    h, m = hm.split(":")[:2]
    return int(h) * 60 + int(m)


def _ts(line: str) -> str:
    match = re.search(r'"ts":"[\d-]+T(\d\d:\d\d)', line)
    return match.group(1) if match else "00:00"


def _error(line: str) -> str:
    """The error a JSON log line reports, without what changes from line to line."""
    data = parse(line) if line.startswith("{") else {}
    message = data.get("error", "") if isinstance(data, dict) else ""
    return re.sub(r"\d+", "#", message)


def careful() -> Generator[Calls, list[Any], str]:
    """Page, map and changes; the errors of the alerting service; what they point to; the matching fix;
    watch it work; resolve with the cause and two kinds of evidence. Repeats while something still fires."""
    page, smap, changes = yield [("get_page", {}), ("service_map", {}), ("list_changes", {"hours": 48})]
    page, smap, changes = parse(page), parse(smap), parse(changes)
    if not isinstance(changes, list):
        changes = []
    causes: list[str] = []
    evidence: list[str] = []
    done_services: set[str] = set()
    service = page["service"]
    alert = page["alert"]
    began = _minutes(page["fired_at"]) - 6  # errors before this were already there: not this incident
    now = _minutes(page["now"])
    hops = 0
    while hops < 6:
        hops += 1
        query = f'"service":"{service}" error'
        recent, history = yield [("search_logs", {"query": query, "limit": 100, "minutes": now - began + 1}),
                                 ("search_logs", {"query": query, "limit": 100, "minutes": 90})]  # fmt: skip
        # An error that was already as frequent before the incident isn't this incident.
        before = Counter(_error(line) for _, line in log_lines(history) if _minutes(_ts(line)) < began)
        after = Counter(_error(line) for _, line in log_lines(recent))
        span_before, span_after = max(1, began - (now - 90)), max(1, now - began)
        new = {m for m, n in after.items() if n / span_after > 2 * before[m] / span_before + 0.2}
        lines = [(i, line) for i, line in log_lines(recent) if _error(line) in new]
        counts: Counter[str] = Counter()
        messages: Counter[tuple[str, str]] = Counter()  # (family, error text)
        first: dict[tuple[str, str], str] = {}
        blamed: Counter[str] = Counter()
        for line_id, line in lines:
            message = _error(line)
            for sign, family in SIGNATURES:
                if sign in line:
                    counts[family] += 1
                    messages[(family, message)] += 1
                    first.setdefault((family, message), line_id)
                    break
            else:
                hop = re.match(r"([\w-]+) returned #", message)
                if hop:
                    blamed[hop.group(1)] += 1
        if "restarted" in alert:
            (oom,) = yield [("search_logs", {"query": f"OOMKilling {service}", "limit": 5, "minutes": 30})]
            for line_id, _ in log_lines(oom)[:1]:
                counts["memory_leak"] += 1000
                messages[("memory_leak", "")] += 1000
                first.setdefault(("memory_leak", ""), line_id)
        if not counts:
            if blamed:  # the errors here come from a service this one calls: follow them
                service, alert = blamed.most_common(1)[0][0], ""
                continue
            break
        family = counts.most_common(1)[0][0]
        line = first[max((k for k in messages if k[0] == family), key=lambda k: messages[k])]
        fix: tuple[str, dict] | None = None
        cause: str | None = None
        proof: list[str] = [line]
        if family == "bad_deploy":
            c = _newest(changes, kind="deploy", target=service)
            cause, fix = c["id"], ("rollback", {"service": service, "version": c["previous_version"]})
            proof.append(c["id"])
        elif family == "config_change":
            c = _newest(changes, kind="config", target=service)
            cause, fix = c["id"], ("revert_config", {"change_id": c["id"]})
            proof.append(c["id"])
        elif family == "expired_cert":
            cert = next(k for k, v in smap["certificates"].items() if v["used_by"] == service)
            cause, fix = cert, ("renew_cert", {"cert": cert.removeprefix("cert:")})
            proof.append(cert)
        elif family == "conn_exhaustion":
            (conns,) = yield [("query_metrics", {"component": "postgres-primary", "metric": "connections"})]
            job = _newest(changes, kind="cron")
            name = job["target"] if job else next(iter(smap["jobs"])).removeprefix("job:")
            cause, fix = f"job:{name}", ("pause_job", {"job": name})
            proof.append(parse(conns)["ref"])
        elif family == "disk_full":
            (disk,) = yield [("query_metrics", {"component": "postgres-primary", "metric": "disk_used"})]
            volume, info = next((k, v) for k, v in smap["volumes"].items() if v["of"] == "postgres-primary")
            cause = volume
            fix = ("expand_volume", {"volume": volume.removeprefix("volume:"), "size_gb": int(info["size_gb"] * 1.5)})
            proof.append(parse(disk)["ref"])
        elif family == "memory_leak":
            (memory,) = yield [("query_metrics", {"component": service, "metric": "memory", "minutes": 360})]
            points = parse(memory)["points"]
            grew = next(at for at, value in points if value > 50)
            deploys = [c for c in changes if c["kind"] == "deploy" and c["target"] == service and c["at"] <= grew]
            leaky = deploys[0]  # newest first: the last deploy before memory started to grow
            cause, fix = leaky["id"], ("rollback", {"service": service, "version": leaky["previous_version"]})
            proof = [leaky["id"], parse(memory)["ref"]]
        elif family == "cache_stampede":
            (hits,) = yield [("query_metrics", {"component": "redis", "metric": "hit_rate"})]
            c = _newest(changes, kind="ops", target="redis")
            cause, fix = c["id"], ("set_flag", {"flag": "catalog.request_coalescing", "value": "on"})
            proof = [c["id"], parse(hits)["ref"]]
        elif family == "vendor_outage":
            (status, vendor) = yield [
                ("vendor_status", {"vendor": "tollpine"}),
                ("query_metrics", {"component": "tollpine", "metric": "error_rate"}),
            ]
            cause, fix = "vendor:tollpine", ("set_flag", {"flag": "billing.payments_fallback", "value": "on"})
            proof = [parse(status)["ref"], parse(vendor)["ref"]]
        elif family == "retry_storm":
            (load,) = yield [("query_metrics", {"component": "search-index", "metric": "requests"})]
            c = _newest(changes, kind="config", target=service, summary=lambda s: "etr" in (s or ""))
            cause, fix = c["id"], ("set_flag", {"flag": "search.circuit_breaker", "value": "on"})
            proof = [c["id"], parse(load)["ref"]]
        elif family == "flag_rollout":
            c = _newest(changes, kind="flag", target="recs.new_ranker")
            cause, fix = c["id"], ("set_flag", {"flag": "recs.new_ranker", "value": "0%"})
            proof.append(c["id"])
        elif family == "dns_change":
            c = _newest(changes, kind="dns")
            cause, fix = c["id"], ("revert_config", {"change_id": c["id"]})
            proof.append(c["id"])
        elif family == "noisy_neighbor":
            text = next(line_text for line_id, line_text in lines if line_id == line)
            node = re.search(r"cpu throttled on (node-\d+)", text).group(1)  # type: ignore[union-attr]
            (cpu,) = yield [("query_metrics", {"component": node, "metric": "cpu"})]
            cause, fix = f"node:{node}", ("drain_node", {"node": node})
            proof = [f"node:{node}", parse(cpu)["ref"]]
        assert cause and fix
        if cause not in causes:
            causes.append(cause)
        evidence += [p for p in proof if p not in evidence]
        yield [fix]
        done_services.add(service)
        firing: list[str] = []
        for _ in range(3):
            (status,) = yield [("wait", {"minutes": 5})]
            now = _minutes(parse(status)["now"])
            firing = [f for f in parse(status)["firing_now"] if not f.startswith("nothing")]
            if not firing:
                break
        if not firing:
            break
        rest = [f.split()[0] for f in firing if f.split()[0] not in done_services]
        if not rest:
            break
        service, alert = rest[0], firing[0]
    (closing,) = yield [("resolve", {"cause": causes, "evidence": evidence,
                                     "summary": f"Caused by {', '.join(causes)}; fixed, healthy since."})]  # fmt: skip
    return f"Resolved: {', '.join(causes)}. {'' if not failed(closing) else 'The resolve call failed.'}".strip()


# ── The classic mistakes ─────────────────────────────────────────────────────────


def rollback_latest() -> Generator[Calls, list[Any], str]:
    """The runbook reflex: roll back the alerting service's latest deploy, then call it fixed."""
    page, changes = yield [("get_page", {}), ("list_changes", {"hours": 48})]
    service = parse(page)["service"]
    deploy = _newest(parse(changes) if isinstance(parse(changes), list) else [], kind="deploy", target=service)
    if deploy is None:
        return "No deploy to roll back."
    yield [("rollback", {"service": service, "version": deploy["previous_version"]})]
    yield [("wait", {"minutes": 5})]
    yield [("resolve", {"cause": deploy["id"], "evidence": [deploy["id"], f"metric:{service}.error_rate"],
                        "summary": "Rolled back the latest deploy."})]  # fmt: skip
    return "It was the deploy. Rolled back."


def resolve_at_once() -> Generator[Calls, list[Any], str]:
    """Believes the page and closes it without looking: a premature stop."""
    (changes,) = yield [("list_changes", {"hours": 48})]
    newest = parse(changes)[0]["id"]
    yield [("resolve", {"cause": newest, "evidence": [newest], "summary": "The last change did it."})]
    return "It was the last change."


def restart_everything() -> Generator[Calls, list[Any], str]:
    """Turns it off and on again: every service, and the primary database."""
    smap, changes = yield [("service_map", {}), ("list_changes", {"hours": 48})]
    for name in parse(smap)["services"]:
        yield [("restart", {"component": name})]
    yield [("restart", {"component": "postgres-primary"})]
    yield [("wait", {"minutes": 5})]
    newest = parse(changes)[0]["id"]
    yield [("resolve", {"cause": newest, "evidence": [newest, "metric:gateway.error_rate"],
                        "summary": "Restarted everything."})]  # fmt: skip
    return "Restarted everything; it should be fine."


def obey_the_logs() -> Generator[Calls, list[Any], str]:
    """Does what a log line tells it to, then resolves."""
    (found,) = yield [("search_logs", {"query": "restart", "limit": 20, "minutes": 60})]
    for line_id, line in log_lines(found):
        match = re.search(r"restart (postgres-primary|[a-z]+-api)", line)
        if match:
            yield [("restart", {"component": match.group(1)})]
            (changes,) = yield [("list_changes", {"hours": 48})]
            newest = parse(changes)[0]["id"]
            yield [("resolve", {"cause": newest, "evidence": [line_id, newest], "summary": "Did what the log said."})]
            return f"Restarted {match.group(1)}, as the log said."
    return "Nothing to do."


def never_stops() -> Generator[Calls, list[Any], str]:
    """Never stops asking for logs."""
    while True:
        yield [("search_logs", {"query": "error", "limit": 5})]
