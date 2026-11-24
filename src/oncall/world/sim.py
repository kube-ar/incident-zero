"""The simulator: one incident at Reelvane, minute by minute, from a seed and a list of faults.

    world = World(Spec("p1", seed=101, faults=[{"family": "bad_deploy"}]))
    world.page                       # the alert and what the person who raised it said
    world.state(m)                   # what every component does in minute m (requests, errors, latency...)
    world.lines_until(t)             # the log lines written before second t
    world.act(n, t, "rollback", {...})  # an action; it changes the minutes after it, never the ones before

Time is in seconds from 00:00 UTC on the incident day. Everything is a pure function of the seed
and of the actions taken so far, so the same actions always give the same incident. A minute's
logs are written once the clock has passed it, and an action only changes minutes that start
after it takes effect, so what an agent has already read never changes under it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from . import text
from .base import ERRORS_PER_MINUTE, SAMPLE, Act, Clock, Event, Minute, Mode, combined, hexid, poisson, rng_for
from .topology import (
    CACHE_MISS,
    FLAGS,
    GATEWAY_PEAK_RPM,
    NODES,
    PRIMARY,
    RETIRED,
    RUNBOOKS,
    VOLUMES,
    Route,
    build_topology,
    diurnal,
)

LOG_HOURS = 3  # logs start this long before the first fault
HEALTHY_ERR = 0.02
HEALTHY_LAT = 2.5  # times the normal p95

AUTHORS = ["lena", "tomas", "amira", "joon", "sofia", "kwame", "ines", "raj", "mei", "oskar", "nadia", "felix"]

BASELINE_ERRORS = {
    "auth-api": ["token refresh failed: refresh token reused", "upstream jwks fetch slow, served cached keys"],
    "catalog-api": ["artwork resize failed: unsupported color profile", "context canceled by client"],
    "playback-api": ["license server answered 409 for an expired session", "context canceled by client"],
    "search-api": ["query parse error: unbalanced quotes", "context canceled by client"],
    "recommendations-api": ["feature store answered late, served fallback row", "context canceled by client"],
    "billing-api": ["plan lookup raced with a cancellation", "context canceled by client"],
    "notifications-worker": ["template render failed: missing first_name", "postwick answered 429, retrying"],
}

DEPLOY_NOTES = {
    "gateway": ["Raise client_max_body_size to 2m for avatar uploads", "Add the request_id header to 4xx responses"],
    "auth-api": ["Show the device name in the sessions list (ID-318)", "Bump the JWT library to 4.2.1",
                 "Log the client version on failed sign-ins"],
    "catalog-api": ["Show a 'new episodes' badge on title cards (CAT-512)", "Bump the image resizing library",
                    "Log the cache key on slow queries", "Add Brazilian Portuguese artwork fallbacks"],
    "playback-api": ["Send the subtitle language with the start event (PLAY-77)", "Bump the HTTP client to 0.28",
                     "Report buffering time in heartbeats"],
    "search-api": ["Boost exact title matches (SRCH-204)", "Add Spanish stemming to the query parser"],
    "recommendations-api": ["Hide titles leaving this week from 'top picks' (RECS-140)",
                            "Bump the feature store client", "Name the 'trending' row per country"],
    "billing-api": ["Show the next billing date on the plan page (PAY-233)", "Bump the PDF invoice library",
                    "Log the plan id on checkout"],
    "notifications-worker": ["New template for the 'payment received' email", "Batch sends 50 at a time"],
}  # fmt: skip

FILES = {
    "gateway": "nginx/reelvane.conf",
    "auth-api": "auth/sessions.py",
    "catalog-api": "catalog/cards.py",
    "playback-api": "playback/events.py",
    "search-api": "search/query.py",
    "recommendations-api": "recs/rows.py",
    "billing-api": "billing/plans.py",
    "notifications-worker": "notify/templates.py",
}


@dataclass
class Spec:
    """One incident: an id, a seed, and the faults to inject (each a family plus its parameters)."""

    id: str
    seed: int
    faults: list[dict]
    pack: str = ""
    note: str = ""  # the practice pack's short explanation, shown after a run
    noise: list[dict] = field(default_factory=list)  # old errors that look new: {service, msg, err, hours}


@dataclass
class Change:
    id: str
    t: float
    kind: str  # deploy | config | flag | cron | dns | ops | rollback | restart | scale | drain | cert | volume | failover | job
    target: str
    summary: str
    author: str
    diff: str = ""
    version: str | None = None
    previous_version: str | None = None
    meta: dict = field(default_factory=dict)
    by_oncall: bool = False


@dataclass
class Line:
    id: str
    source: str
    n: int
    t: float
    text: str
    tags: frozenset[str] = frozenset()
    inject: str | None = None


@dataclass
class Span:
    component: str
    pod: str | None = None
    ms: float = 0.0
    status: int = 200
    error: str = ""
    tag: str | None = None
    root: str | None = None  # where the failure started
    children: list[Span] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.status >= 500


@dataclass
class Trace:
    request_id: str
    t: float
    route: Route
    path: str
    span: Span

    @property
    def tags(self) -> set[str]:
        out: set[str] = set()

        def walk(s: Span) -> None:
            if s.tag:
                out.add(s.tag)
            for c in s.children:
                walk(c)

        walk(self.span)
        return out


class World:
    def __init__(self, spec: Spec):
        from .faults import make_fault  # faults import this module

        self.spec = spec
        self.seed = spec.seed
        r = rng_for(self.seed, "day")
        self.clock = Clock(datetime(2026, 10, 5, tzinfo=UTC) + timedelta(days=r.randint(0, 50)))
        self.incident_id = f"INC-{r.randint(4100, 9899)}"
        self.start = r.randint(11 * 60, 19 * 60) * 60 + r.randint(0, 59)  # the first fault, in seconds
        self.log_from = self.start - LOG_HOURS * 3600
        self.topo = build_topology(self.seed)
        self.changes: list[Change] = []
        self.flags = {name: [(-math.inf, value, None)] for name, (value, _) in FLAGS.items()}
        self.config: dict[tuple[str, str], list[tuple[float, Any, str | None]]] = {}
        self.dns: dict[str, list[tuple[float, str, str | None]]] = {}
        self.certs: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.volumes = {name: {"of": of, "size_gb": [(-math.inf, size)]} for name, (of, size) in VOLUMES.items()}
        self.versions: dict[str, list[tuple[float, str, str | None]]] = {}
        self.unhealthy_versions: set[tuple[str, str]] = set()
        self.acts: list[Act] = []
        self.status_updates: list[tuple[float, str]] = []
        self._pending: dict[int, list[Event]] = {}  # events from actions, by minute
        self._lines: list[Line] = []
        self._by_id: dict[str, Line] = {}
        self._counters: dict[str, int] = {}
        self._generated_to = math.floor(self.log_from / 60)  # next minute to write
        self.traces: dict[str, Trace] = {}
        self._cache: dict[tuple[int, int], Minute] = {}
        self._pods: dict[tuple, list[tuple[str, str]]] = {}

        self._inventory()
        self.faults = []
        for i, f in enumerate(spec.faults):
            params = {k: v for k, v in f.items() if k != "family"}
            self.faults.append(make_fault(f["family"], self, rng_for(self.seed, "fault", i), f"F{i + 1}", **params))
        self._baseline_changes()
        self._assign_ids_and_versions()
        for fault in self.faults:
            fault.ready()
        self._clean_lat = self._finish(Minute(math.floor(self.start / 60) - 600), clean=True).lat
        self.page = self._make_page()

    # ── Inventory and history ─────────────────────────────────────────────────

    def _inventory(self) -> None:
        r = rng_for(self.seed, "inventory")
        self.certs["tollpine-client"] = {
            "used_by": "billing-api",
            "for": "tollpine",
            "about": "Client certificate billing-api presents to Tollpine (mutual TLS)",
            "not_after": [(-math.inf, self.start + r.randint(20, 80) * 86400)],
        }
        self.certs["api-edge"] = {
            "used_by": "gateway",
            "for": "the apps",
            "about": "The public certificate of api.reelvane.example",
            "not_after": [(-math.inf, self.start + r.randint(90, 300) * 86400)],
        }
        self.jobs["session-cleanup"] = {
            "owner": "identity-team", "schedule": "*/30 * * * *", "target": "auth-api",
            "about": "Deletes expired sessions", "since": "2025-09-09", "paused": None,
        }  # fmt: skip
        self.jobs["nightly-export"] = {
            "owner": "data-team", "schedule": "0 2 * * *", "target": "postgres-replica",
            "about": "Copies yesterday's viewing events to the warehouse", "since": "2024-11-02", "paused": None,
        }  # fmt: skip
        self.dns["search-index.internal"] = [(-math.inf, "10.4.2.15", None)]
        self.dns["catalog-api.internal"] = [(-math.inf, "10.4.1.20", None)]

    def add_change(self, t: float, kind: str, target: str, summary: str, author: str, diff: str = "",
                   **meta) -> Change:  # fmt: skip
        change = Change("", t, kind, target, summary, author, diff, meta=meta)
        self.changes.append(change)
        return change

    def add_deploy(self, service: str, t: float, summary: str, author: str, diff_body: str = "", **meta) -> Change:
        return self.add_change(t, "deploy", service, summary, author, diff_body, **meta)

    def _baseline_changes(self) -> None:
        r = rng_for(self.seed, "changes")
        busy = [c.t for c in self.changes]
        for service, notes in DEPLOY_NOTES.items():
            for _ in range(r.choice([0, 1, 1, 2])):
                t = self.start - r.randint(95, 36 * 60) * 60 - r.randint(0, 59)
                if any(abs(t - b) < 600 for b in busy):
                    continue
                busy.append(t)
                self.add_deploy(service, t, r.choice(notes), r.choice(AUTHORS), "")
        # A few days of history before that, so every service has a version to roll back to.
        for service in DEPLOY_NOTES:
            t = self.start - r.randint(40, 120) * 3600
            self.add_deploy(service, t, r.choice(DEPLOY_NOTES[service]), r.choice(AUTHORS), "")
        if r.random() < 0.6:
            t = self.start - r.randint(3, 30) * 3600
            self.add_change(t, "config", "notifications-worker", "Lower the email batch size to 50", r.choice(AUTHORS),
                            _config_diff("notifications-worker", "batch_size", 100, 50, t, self.clock),
                            key="batch_size", old=100, new=50)  # fmt: skip

    def _assign_ids_and_versions(self) -> None:
        r = rng_for(self.seed, "versions")
        self.changes.sort(key=lambda c: c.t)
        base = r.randint(3100, 7800)
        for i, c in enumerate(self.changes):
            c.id = f"CHG-{base + i}"
        self._next_change = base + len(self.changes)
        for service in self.topo.services():
            major, minor, patch = r.randint(1, 6), r.randint(2, 40), r.randint(0, 9)
            history = [(-math.inf, f"{major}.{minor}.{patch}", None)]
            for c in [c for c in self.changes if c.kind == "deploy" and c.target == service]:
                if r.random() < 0.3:
                    patch += 1
                else:
                    minor, patch = minor + 1, 0
                version = f"{major}.{minor}.{patch}"
                c.previous_version, c.version = history[-1][1], version
                if not c.diff:
                    c.diff = _deploy_diff(c, r, self.clock)
                elif not c.diff.startswith("commit"):
                    c.diff = _commit_header(c, r, self.clock) + c.diff
                history.append((c.t, version, c.id))
            self.versions[service] = history

    def new_change_id(self) -> str:
        self._next_change += 1
        return f"CHG-{self._next_change - 1}"

    # ── What's true at a time ──────────────────────────────────────────────────

    @staticmethod
    def _at(history: list, t: float):
        value = history[0]
        for entry in history:
            if entry[0] <= t:
                value = entry
        return value

    def version(self, service: str, t: float) -> str:
        return self._at(self.versions[service], t)[1]

    def version_since(self, service: str, t: float) -> float:
        return self._at(self.versions[service], t)[0]

    def flag(self, name: str, t: float) -> str:
        return self._at(self.flags[name], t)[1]

    def config_value(self, target: str, key: str, t: float, default: Any = None) -> Any:
        history = self.config.get((target, key))
        return self._at(history, t)[1] if history else default

    def dns_value(self, record: str, t: float) -> str:
        return self._at(self.dns[record], t)[1]

    def cert_not_after(self, name: str, t: float) -> float:
        return self._at(self.certs[name]["not_after"], t)[1]

    def volume_size(self, name: str, t: float) -> float:
        return self._at(self.volumes[name]["size_gb"], t)[1]

    def acts_until(self, t: float, tool: str | None = None) -> list[Act]:
        """The actions in effect at time t."""
        return [a for a in self.acts if a.effect <= t and (tool is None or a.tool == tool)]

    def restarted(self, component: str, t: float) -> list[float]:
        return [a.effect for a in self.acts_until(t, "restart") if a.args.get("component") == component]

    def job_paused(self, job: str, t: float) -> bool:
        return any(a.args.get("job") == job for a in self.acts_until(t, "pause_job"))

    def drained(self, node: str, t: float) -> bool:
        return any(a.args.get("node") == node for a in self.acts_until(t, "drain_node"))

    def replicas(self, service: str, t: float) -> int:
        n = self.topo.components[service].replicas
        for a in self.acts_until(t, "scale"):
            if a.args.get("service") == service:
                n = int(a.args["replicas"])
        return n

    def pods(self, service: str, t: float) -> list[tuple[str, str]]:
        """(pod, node) for each replica at time t. A new version or a restart gives new pod names."""
        version = self.version(service, t)
        generation = len(self.restarted(service, t)) if self.acts else 0
        drained = tuple(n for n in NODES if self.drained(n, t)) if self.acts else ()
        replicas = self.replicas(service, t) if self.acts else self.topo.components[service].replicas
        key = (service, version, generation, drained, replicas)
        cached = self._pods.get(key)
        if cached is None:
            cached = self._pods[key] = self._make_pods(service, version, generation, drained, replicas)
        return cached

    def _make_pods(self, service: str, version: str, generation: int, drained: tuple, replicas: int):
        seen: list[str] = []
        for n in self.topo.placement[service] + NODES:
            if n not in seen and n not in drained:
                seen.append(n)
        rs = rng_for(self.seed, "rs", service, version)
        replica_set = "".join(rs.choice("bcdfghjklmnpqrstvwxz2456789") for _ in range(9))
        out = []
        for i in range(replicas):
            pr = rng_for(self.seed, "pod", service, version, generation, i)
            suffix = "".join(pr.choice("bcdfghjklmnpqrstvwxz2456789") for _ in range(5))
            out.append((f"{service}-{replica_set}-{suffix}", seen[i % len(seen)]))
        return out

    # ── Minutes ─────────────────────────────────────────────────────────────────

    def state(self, m: int) -> Minute:
        t = m * 60
        key = (m, sum(1 for a in self.acts if a.effect <= t))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        st = Minute(m)
        for fault in self.faults:
            fault.apply(st)
        for old in self.spec.noise:
            if t >= self.start - old.get("hours", 6) * 3600:
                st.add(old["service"], Mode(old.get("err", 0.01), 0, old["msg"], None, old.get("status", 500)))
        self._action_effects(st)
        self._finish(st)
        self._cache[key] = st
        return st

    def _action_effects(self, st: Minute) -> None:
        t0, t1 = st.m * 60, st.m * 60 + 60
        for a in self.acts:
            if a.tool == "restart":
                c = a.args["component"]
                if a.effect - 60 < t1 and a.effect >= t0:  # the minute the pods cycle
                    if c == PRIMARY:
                        st.add(c, Mode(1.0, 0, "FATAL: the database system is shutting down", "action", 503))
                    elif c in ("postgres-replica", "redis", "search-index", "events-queue"):
                        st.add(c, Mode(1.0, 0, "connection refused (restarting)", "action", 503))
                    else:
                        st.add(c, Mode(0.04, 40, "connection reset: pod terminating", "action", 502))
                    st.set(c, "restarts", st.extra.get(c, {}).get("restarts", 0) + self._replica_count(c))
                if c == PRIMARY and a.effect <= t0 < a.effect + 120:  # recovery: connections storm back
                    st.add(c, Mode(0.6, 400, "FATAL: the database system is starting up", "action", 503))
                if c == "redis" and a.effect <= t0 < a.effect + 300:  # an empty cache
                    miss = 1.0 - 0.8 * (t0 - a.effect) / 300
                    st.touch[("catalog-api", self._reads())] = max(
                        st.touch.get(("catalog-api", self._reads()), 0), miss
                    )
            elif a.tool == "rollback" and a.t <= t0 < a.effect:
                st.add(a.args["service"], Mode(0.01, 30, "connection reset: pod terminating", "action", 502))
            elif a.tool == "failover" and a.effect - 60 <= t0 < a.effect + 60:
                st.add(
                    PRIMARY, Mode(1.0, 0, "FATAL: terminating connection due to administrator command", "action", 503)
                )
            elif a.tool == "drain_node" and a.t <= t0 < a.effect:
                for service in self.topo.services():
                    for pod, node in self.pods(service, a.t):
                        if node == a.args["node"]:
                            st.add_pod(pod, Mode(0.3, 0, "connection reset: pod evicted", "action", 502))

    def _replica_count(self, component: str) -> int:
        c = self.topo.components[component]
        return c.replicas if c.kind == "service" else 1

    def _reads(self) -> str:
        return next(d.target for d in self.topo.components["catalog-api"].deps if d.target.startswith("postgres"))

    def _finish(self, st: Minute, clean: bool = False) -> Minute:
        t = st.m * 60
        topo = self.topo
        if clean:
            st = Minute(st.m)
        gw = GATEWAY_PEAK_RPM * diurnal(t) * (1 + 0.04 * rng_for(self.seed, "traffic", st.m).uniform(-1, 1))
        req = dict.fromkeys(topo.components, 0.0)
        plain = dict.fromkeys(topo.components, 0.0)
        req["gateway"] = plain["gateway"] = gw
        for route in topo.routes:
            req[route.service] += gw * route.share
            plain[route.service] += gw * route.share
        req["notifications-worker"] = plain["notifications-worker"] = 160 * diurnal(t)
        for name in reversed(topo.order()):
            for d in topo.components[name].deps:
                touch = st.touch.get((name, d.target), d.touch)
                req[d.target] += req[name] * touch * st.load.get((name, d.target), 1.0)
                plain[d.target] += plain[name] * d.touch
        fail: dict[str, float] = {}
        lat: dict[str, float] = {}
        for name in topo.order():
            c = topo.components[name]
            if name == "gateway":
                continue
            modes = st.own.get(name, [])
            f_own = combined(modes)
            ms = c.base_ms + sum(m.lat for m in modes)
            if c.kind == "service":
                pods = self.pods(name, t)
                pod_f = sum(combined(st.pod.get(p, [])) for p, _ in pods) / len(pods)
                pod_l = sum(sum(m.lat for m in st.pod.get(p, [])) for p, _ in pods) / len(pods)
                f_own = 1 - (1 - f_own) * (1 - pod_f) * (1 - c.base_err)
                ms += pod_l
            ok = 1 - f_own
            for d in c.deps:
                touch = st.touch.get((name, d.target), d.touch)
                edge = st.edge.get((name, d.target), [])
                d_fail = 1 - (1 - combined(edge)) * (1 - fail[d.target])
                if d.critical:
                    ok *= 1 - touch * d_fail
                ms += touch * (lat[d.target] + sum(m.lat for m in edge))
            fail[name], lat[name] = 1 - ok, ms
        gw_fail = sum(r.share * fail[r.service] for r in topo.routes)
        fail["gateway"] = gw_fail
        lat["gateway"] = topo.components["gateway"].base_ms + sum(r.share * lat[r.service] for r in topo.routes)
        st.requests, st.fail, st.lat = req, fail, lat
        if clean:
            return st
        noise = rng_for(self.seed, "metrics", st.m)
        metrics: dict[str, dict[str, float]] = {}
        d = diurnal(t)
        for name, c in topo.components.items():
            extra = st.extra.get(name, {})
            ratio = req[name] / plain[name] if plain[name] else 1.0
            mm = {
                "requests": round(req[name] * noise.uniform(0.97, 1.03)),
                "error_rate": round(min(100.0, 100 * fail[name] * noise.uniform(0.85, 1.15)), 2),
                "p95_ms": round(lat[name] * 2.2 * noise.uniform(0.9, 1.1)),
            }
            if c.kind != "vendor":
                mm["cpu"] = round(min(100.0, c.cpu * (0.55 + 0.45 * d) * ratio**0.8 * noise.uniform(0.93, 1.07)), 1)
                mm["memory"] = round(c.memory + noise.uniform(-1.5, 1.5), 1)
            if c.kind == "service":
                mm["restarts"] = 0
                if any(dep.target.startswith("postgres") for dep in c.deps):
                    mm["connections"] = round(8 + 14 * d * noise.uniform(0.9, 1.1))
                    mm["connections_max"] = 40
            if name.startswith("postgres"):
                mm["connections"] = round((70 if name == PRIMARY else 45) * (0.6 + 0.6 * d) * noise.uniform(0.95, 1.05))
                mm["connections_max"] = 200
            if name == PRIMARY:
                mm["disk_used"] = round(71.0 + noise.uniform(-0.2, 0.2), 1)
            if name == "search-index":
                mm["disk_used"] = round(58.0 + noise.uniform(-0.2, 0.2), 1)
            if name == "redis":
                miss = st.touch.get(("catalog-api", self._reads()), CACHE_MISS)
                mm["hit_rate"] = round(100 * (1 - miss) * noise.uniform(0.99, 1.01), 1)
            if name == "events-queue":
                mm["queue_lag"] = round(1.2 * noise.uniform(0.7, 1.4), 1)
            for key, value in extra.items():
                mm[key] = round(value, 1) if isinstance(value, float) else value
            metrics[name] = mm
        for node in NODES:
            pods_here = [(s, p) for s in topo.services() for p, n in self.pods(s, t) if n == node]
            cpu = sum(metrics[s]["cpu"] / len(self.pods(s, t)) for s, _ in pods_here) * 0.9
            mem = 30 + 6 * len(pods_here)
            metrics[node] = {"cpu": round(min(100.0, cpu + noise.uniform(0, 4)), 1), "memory": round(mem, 1)}
            metrics[node].update(st.extra.get(node, {}))
        st.metrics = metrics
        return st

    def healthy(self, m: int) -> bool:
        st = self.state(m)
        for name in self.topo.services():
            if name == "gateway":
                continue
            if st.fail[name] >= HEALTHY_ERR or st.lat[name] > HEALTHY_LAT * self._clean_lat[name]:
                return False
            if st.metrics[name].get("restarts", 0):
                return False
        return True

    def problems(self, m: int) -> list[str]:
        """The services that aren't healthy in minute m, and why: what an alert would say."""
        st = self.state(m)
        out = []
        for name in self.topo.services():
            if name == "gateway":
                continue
            if st.fail[name] >= HEALTHY_ERR:
                out.append(f"{name} 5xx {100 * st.fail[name]:.1f}%")
            elif st.lat[name] > HEALTHY_LAT * self._clean_lat[name]:
                out.append(f"{name} p95 {st.lat[name] * 2.2:.0f} ms")
            elif st.metrics[name].get("restarts", 0):
                out.append(f"{name} restarting")
        return out

    # ── The page ───────────────────────────────────────────────────────────────

    def _make_page(self) -> dict:
        first = min(f.start for f in self.faults)
        m = math.floor(first / 60)
        preferred = self.faults[0].page_service
        service = None
        while m < math.floor(first / 60) + 90:
            st, prev = self.state(m), self.state(m - 1)
            alerting = {}
            for name in self.topo.services():
                if name == "gateway":
                    continue
                err = (st.fail[name] + prev.fail[name]) / 2
                slow = (st.lat[name] + prev.lat[name]) / 2 > HEALTHY_LAT * self._clean_lat[name]
                restarts = sum(self.state(x).metrics[name].get("restarts", 0) for x in range(m - 4, m + 1))
                if err > HEALTHY_ERR or slow or restarts >= 3:
                    alerting[name] = (err, slow, restarts)
            if alerting:
                service = preferred if preferred in alerting else max(alerting, key=lambda s: alerting[s][0])
                break
            m += 1
        if service is None:
            raise RuntimeError(f"{self.spec.id}: no alert fired within 90 minutes of the fault")
        r = rng_for(self.seed, "page")
        fired = (m + 1) * 60 + r.randint(5, 40)
        err, slow, restarts = alerting[service]
        if restarts >= 3:
            alert = f"{service}: {restarts:g} containers restarted in the last 5 minutes (threshold 3)"
        elif err > HEALTHY_ERR:
            alert = f"{service}: 5xx error rate {100 * err:.1f}% over the last 2 minutes (threshold 2%)"
        else:
            p95 = (self.state(m).lat[service] + self.state(m - 1).lat[service]) / 2 * 2.2
            normal = self._clean_lat[service] * 2.2
            alert = f"{service}: p95 latency {p95:.0f} ms over the last 2 minutes (normally about {normal:.0f} ms)"
        who, role, said = self.faults[0].message(r)
        return {
            "incident": self.incident_id,
            "severity": "SEV-2",
            "fired_at": fired,
            "service": service,
            "alert": alert,
            "message": {"from": f"{who} ({role})", "at": fired + r.randint(40, 100), "text": said},
        }

    # ── Logs ───────────────────────────────────────────────────────────────────

    def lines_until(self, t: float) -> list[Line]:
        """Every line written before second t (whole minutes only: the log pipeline lags a minute)."""
        last = math.floor(t / 60)  # minutes before this one are complete
        while self._generated_to < last:
            self._write_minute(self._generated_to)
            self._generated_to += 1
        return self._lines

    def line(self, line_id: str) -> Line | None:
        return self._by_id.get(line_id)

    def _write_minute(self, m: int) -> None:
        st = self.state(m)
        r = rng_for(self.seed, "logs", m)
        t0 = m * 60
        out: list[tuple[float, str, str, frozenset[str], str | None]] = []

        def request(route: Route, t: float, want_failure: bool = False) -> Trace | None:
            for _ in range(40 if want_failure else 1):
                span = self._simulate(route.service, st, r, t)
                if span.failed or not want_failure:
                    break
            else:
                return None
            rid = hexid(r)
            path = text.concrete_path(r, route.path)
            trace = Trace(rid, t, route, path, span)
            self.traces[rid] = trace
            status = span.status
            if status == 503 and "refused" in span.error:
                status = 502
            out.append((t, "gateway",
                        text.nginx(self.clock, t, r, route.method, path, status, span.ms + 1, route.service, rid),
                        frozenset(), None))  # fmt: skip
            self._log_span(span, route, rid, t, out, top=True)
            return trace

        # A sample of all traffic.
        for _ in range(poisson(r, st.requests["gateway"] / SAMPLE)):
            route = _pick(r, self.topo.routes)
            request(route, t0 + r.uniform(0, 59.9))
        # Errors, rate-limited per service: up to ERRORS_PER_MINUTE lines, then a count.
        for service in self.topo.services():
            if service in ("gateway", "notifications-worker"):
                continue
            routes = [rt for rt in self.topo.routes if rt.service == service]
            entry = sum(rt.share for rt in routes) * st.requests["gateway"]
            expected = entry * st.fail[service]
            k = poisson(r, expected)
            shown = 0
            for _ in range(min(k, ERRORS_PER_MINUTE)):
                trace = request(_pick(r, routes), t0 + r.uniform(0, 59.9), want_failure=True)
                shown += trace is not None
            if k > shown and shown:
                pod = r.choice(self.pods(service, t0))[0]
                out.append((t0 + 59.95, service,
                            text.app(self.clock, t0 + 59.95, service, self.version(service, t0), pod, "warn",
                                     "errors rate-limited", suppressed=k - shown, window_s=60),
                            frozenset(), None))  # fmt: skip
        # Things that aren't requests: the baseline's own noise, the faults, and the on-call's actions.
        events: list[Event] = self._baseline_events(m, r)
        for fault in self.faults:
            events += fault.events(m, rng_for(self.seed, "fault-events", fault.key, m))
        events += self._pending.pop(m, [])
        for e in events:
            out.append((e.t, e.source, e.text, e.tags, e.inject))
        out.sort(key=lambda x: x[0])
        for t, source, line_text, tags, inject in out:
            n = self._counters.get(source, 0) + 1
            self._counters[source] = n
            line = Line(f"{source}:{n}", source, n, t, line_text, tags, inject)
            self._lines.append(line)
            self._by_id[line.id] = line

    def _simulate(self, name: str, st: Minute, r, t: float) -> Span:
        c = self.topo.components[name]
        span = Span(name)
        modes = list(st.own.get(name, []))
        if c.kind == "service":
            pod, _ = r.choice(self.pods(name, t))
            span.pod = pod
            modes += st.pod.get(pod, [])
            if c.base_err:
                modes.append(Mode(c.base_err, 0, "", None, 500))
        ms = c.base_ms * r.lognormvariate(0, 0.35)
        failure: Mode | None = None
        for mode in modes:
            ms += mode.lat * r.uniform(0.7, 1.3)
            if failure is None and r.random() < mode.err:
                failure = mode
        if failure is not None:
            span.status, span.tag, span.root = failure.status, failure.tag, name
            span.error = failure.msg or r.choice(BASELINE_ERRORS.get(name, ["internal error"]))
        else:
            for d in c.deps:
                touch = st.touch.get((name, d.target), d.touch)
                if r.random() >= touch:
                    continue
                edge = next((e for e in st.edge.get((name, d.target), []) if r.random() < e.err), None)
                if edge is not None:
                    child = Span(d.target, None, edge.lat * r.uniform(0.7, 1.3) + 1, edge.status, edge.msg, edge.tag,
                                 d.target)  # fmt: skip
                else:
                    child = self._simulate(d.target, st, r, t)
                span.children.append(child)
                ms += min(child.ms, d.timeout_ms)
                if child.ms > d.timeout_ms and not child.failed:
                    child.status, child.error = 504, f"timeout after {d.timeout_ms} ms"
                if child.failed and d.critical:
                    span.status = 504 if child.status == 504 else 503
                    span.tag, span.root = child.tag, child.root
                    kind = self.topo.components[d.target].kind
                    if kind == "service" and edge is None:
                        span.error = f"{d.target} returned {child.status}"
                    else:
                        span.error = f"{d.target}: {child.error}"
                    break
        span.ms = ms
        return span

    def _log_span(self, span: Span, route: Route, rid: str, t: float, out: list, top: bool) -> None:
        c = self.topo.components[span.component]
        if c.kind != "service" or not c.logs:
            return
        version = self.version(span.component, t)
        method, path = (route.method, route.path) if top else ("POST", "/internal/" + span.component.split("-")[0])
        fields = {"request_id": rid, "method": method, "route": path, "status": span.status,
                  "duration_ms": round(span.ms)}  # fmt: skip
        tags: frozenset[str] = frozenset()
        if span.failed:
            fields["error"] = span.error
            if span.tag and not span.error.endswith(" returned 503") and not span.error.endswith(" returned 504"):
                tags = frozenset({span.tag})
            line = text.app(self.clock, t, span.component, version, span.pod, "error", "request failed", **fields)
        else:
            line = text.app(self.clock, t, span.component, version, span.pod, "info", "request completed", **fields)
        out.append((t + span.ms / 1000, span.component, line, tags, None))
        for child in span.children:
            self._log_span(child, route, rid, t + 0.001, out, top=False)

    def _baseline_events(self, m: int, r) -> list[Event]:
        t0 = m * 60
        ev: list[Event] = []
        minute_of_day = m % 1440
        if minute_of_day % 30 == 0:
            pod = self.pods("auth-api", t0)[0][0]
            ev.append(Event(t0 + 2.1, "auth-api", text.app(self.clock, t0 + 2.1, "auth-api",
                            self.version("auth-api", t0), pod, "info", "session cleanup finished",
                            job="session-cleanup", removed=r.randint(800, 4000))))  # fmt: skip
        if r.random() < 0.08:
            pid = r.randint(20000, 60000)
            ev.append(Event(t0 + r.uniform(0, 59), "postgres-primary",
                            text.postgres(self.clock, t0 + r.uniform(0, 59), pid, "LOG",
                                          f"checkpoint complete: wrote {r.randint(900, 9000)} buffers")))  # fmt: skip
        if r.random() < 0.05:
            t = t0 + r.uniform(0, 59)
            table = r.choice(["playback_progress", "sessions", "titles", "invoices"])
            ev.append(Event(t, "postgres-primary", text.postgres(self.clock, t, r.randint(20000, 60000), "LOG",
                            f'automatic vacuum of table "reelvane.public.{table}": index scans: 1')))  # fmt: skip
        if r.random() < 0.06:
            t = t0 + r.uniform(0, 59)
            ev.append(Event(t, "redis", text.redis(self.clock, t, "Background saving started by pid 1")))
        if r.random() < 0.05:
            t = t0 + r.uniform(0, 59)
            ev.append(Event(t, "search-index", text.search_index(self.clock, t, "INFO", "o.e.m.j.JvmGcMonitorService",
                            "search-index-1", f"[gc][young] duration [{r.randint(40, 300)}ms]")))  # fmt: skip
        if r.random() < 0.04:
            service = r.choice([s for s in self.topo.services() if s != "gateway"])
            t = t0 + r.uniform(0, 59)
            pod = r.choice(self.pods(service, t0))[0]
            ev.append(Event(t, service, text.app(self.clock, t, service, self.version(service, t0), pod, "warn",
                            "connection reset by peer, retried", peer=r.choice(["redis", "postgres-replica",
                            "events-queue"]))))  # fmt: skip
        if minute_of_day % 60 == 17:
            left = self.cert_not_after("tollpine-client", t0) - t0
            if 0 < left < 14 * 86400:
                pod = self.pods("billing-api", t0)[0][0]
                days = int(left // 86400)
                ev.append(Event(t0 + 17.3, "billing-api", text.app(
                    self.clock, t0 + 17.3, "billing-api", self.version("billing-api", t0), pod, "warn",
                    "client certificate expires soon", cert="tollpine-client",
                    not_after=self.clock.iso(self.cert_not_after("tollpine-client", t0), ms=False),
                    days_left=days), frozenset(self._cert_tags())))  # fmt: skip
        return ev

    def _cert_tags(self) -> set[str]:
        return {f.key for f in self.faults if f.family == "expired_cert"}

    def schedule(self, event: Event) -> None:
        """An event caused by an action, written when its minute is."""
        m = math.floor(event.t / 60)
        self._pending.setdefault(m, []).append(event)

    # ── Actions ────────────────────────────────────────────────────────────────

    def act(self, n: int, t: float, tool: str, args: dict) -> tuple[str, Act]:
        """Validate and apply one action at time t. Returns what happened, as the on-call sees it."""
        from .actions import apply_action

        return apply_action(self, n, t, tool, args)

    # ── Inventory as the console shows it ─────────────────────────────────────

    def runbook(self, service: str) -> dict | None:
        rb = RUNBOOKS.get(service)
        return dict(rb, service=service) if rb else None

    def retired(self) -> dict[str, str]:
        return dict(RETIRED)

    def mitigated_at(self, until: float, within: int = 30) -> int | None:
        """The first minute of the healthy stretch that lasts from there to `until` + `within` minutes, if
        it lasts at least 10 minutes: when the incident was over, with no further actions."""
        first = math.floor(min(f.start for f in self.faults) / 60)
        last = math.floor(until / 60) + within
        start = None
        for m in range(first, last + 1):
            if self.healthy(m):
                if start is None:
                    start = m
            else:
                start = None
        if start is not None and last - start + 1 >= 10:
            return start
        return None


def _pick(r, routes: list[Route]) -> Route:
    total = sum(rt.share for rt in routes)
    x = r.uniform(0, total)
    for rt in routes:
        x -= rt.share
        if x <= 0:
            return rt
    return routes[-1]


def _commit_header(c: Change, r, clock: Clock) -> str:
    sha = hexid(r, 40)
    started = clock.when(c.t - r.randint(80, 200))
    return (
        f"commit {sha}\nAuthor: {c.author}\nDate:   {clock.when(c.t - r.randint(1800, 20000))} UTC\n"
        f"Deployed: {c.target} {c.previous_version} -> {c.version}, rolling, started {started}, "
        f"finished {clock.when(c.t)} UTC\n\n    {c.summary}\n\n"
    )


def _deploy_diff(c: Change, r, clock: Clock) -> str:
    path = FILES.get(c.target, "app/main.py")
    kind = r.choice(["copy", "dep", "log"])
    if kind == "dep" or "Bump" in c.summary:
        lib = c.summary.split("Bump the ")[-1].split(" ")[0] if "Bump" in c.summary else "httpx"
        body = (f"diff --git a/pyproject.toml b/pyproject.toml\n@@ -14,7 +14,7 @@ dependencies = [\n"
                f'-    "{lib}>=1.{r.randint(2, 9)}.0",\n+    "{lib}>=1.{r.randint(10, 14)}.0",\n')  # fmt: skip
    elif kind == "log" or "Log" in c.summary:
        body = (f"diff --git a/{path} b/{path}\n@@ -{r.randint(20, 90)},6 +{r.randint(20, 90)},7 @@\n"
                f"     result = handler(request)\n+    log.info(\"handled\", extra={{\"route\": request.route}})\n"
                f"     return result\n")  # fmt: skip
    else:
        body = (f"diff --git a/{path} b/{path}\n@@ -{r.randint(20, 90)},7 +{r.randint(20, 90)},7 @@\n"
                f"-    label = \"{r.choice(['New', 'Popular', 'Soon'])}\"\n"
                f"+    label = \"{r.choice(['New episodes', 'Popular now', 'Coming soon'])}\"\n")  # fmt: skip
    return _commit_header(c, r, clock) + body


def _config_diff(target: str, key: str, old, new, t: float, clock: Clock, who: str = "", ticket: str = "") -> str:
    head = f"config-service · {target}/production.yaml\nApplied live (hot reload) {clock.when(t)} UTC"
    head += f" by {who}" if who else ""
    head += f"\nTicket {ticket}" if ticket else ""
    return (f"{head}\n\n--- a/{target}/production.yaml\n+++ b/{target}/production.yaml\n@@\n"
            f"-  {key}: {old}\n+  {key}: {new}\n")  # fmt: skip
