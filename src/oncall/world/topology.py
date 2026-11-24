"""Reelvane's production: services, datastores, vendors, nodes, and how requests flow between them.

The shape is mostly fixed, with a few edges that vary by seed (whether catalog-api reads from the
replica, whether recommendations calls catalog-api...), so an agent can't learn one map by heart.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .base import rng_for


@dataclass(frozen=True)
class Dep:
    target: str
    touch: float  # share of this component's requests that call the target
    critical: bool  # a failure there fails the request (otherwise the caller falls back)
    timeout_ms: int = 2000


@dataclass
class Component:
    name: str
    kind: str  # service | datastore | vendor
    owner: str
    about: str
    base_ms: float
    deps: list[Dep] = field(default_factory=list)
    replicas: int = 0  # services only
    cpu: float = 35.0  # baseline %
    memory: float = 48.0  # baseline %
    base_err: float = 0.002  # baseline share of 5xx
    logs: bool = True  # writes JSON request logs


@dataclass(frozen=True)
class Route:
    method: str
    path: str  # with {placeholders}
    service: str
    share: float  # of the gateway's traffic


ROUTES = [
    Route("GET", "/v1/titles/{id}", "catalog-api", 0.16),
    Route("GET", "/v1/browse/{genre}", "catalog-api", 0.08),
    Route("GET", "/v1/rows/{row}", "recommendations-api", 0.14),
    Route("GET", "/v1/search", "search-api", 0.08),
    Route("POST", "/v1/play/{id}/start", "playback-api", 0.07),
    Route("POST", "/v1/play/heartbeat", "playback-api", 0.25),
    Route("POST", "/v1/auth/token", "auth-api", 0.09),
    Route("GET", "/v1/account", "auth-api", 0.05),
    Route("GET", "/v1/billing/plan", "billing-api", 0.05),
    Route("POST", "/v1/checkout", "billing-api", 0.03),
]

GATEWAY_PEAK_RPM = 4200  # requests per minute at the evening peak

SERVICE_ABOUT = {
    "gateway": "The public API in front of every Reelvane app (nginx). Routes each request to one service.",
    "auth-api": "Sign-in, tokens and account profiles.",
    "catalog-api": "Titles, artwork and browse pages. Cache first (redis), database on a miss.",
    "playback-api": "Starts streams and records progress (heartbeats).",
    "search-api": "Title search.",
    "recommendations-api": "The personalized rows on the home screen.",
    "billing-api": "Plans and checkout. Card payments go to Tollpine.",
    "notifications-worker": "Sends emails from the events queue through Postwick.",
    "postgres-primary": "The main PostgreSQL database: every write goes here.",
    "postgres-replica": "A read-only copy of postgres-primary, for reads that can be a second behind.",
    "redis": "The shared cache and session store.",
    "search-index": "The search cluster that search-api queries.",
    "events-queue": "The queue between billing-api and notifications-worker.",
    "tollpine": "Outside vendor: card payments.",
    "postwick": "Outside vendor: email delivery.",
}

OWNERS = {
    "gateway": "platform-team",
    "auth-api": "identity-team",
    "catalog-api": "catalog-team",
    "playback-api": "playback-team",
    "search-api": "search-team",
    "recommendations-api": "recs-team",
    "billing-api": "payments-team",
    "notifications-worker": "messaging-team",
    "postgres-primary": "dba-team",
    "postgres-replica": "dba-team",
    "redis": "platform-team",
    "search-index": "search-team",
    "events-queue": "platform-team",
    "tollpine": "Tollpine (vendor)",
    "postwick": "Postwick (vendor)",
}

NODES = [f"node-{i}" for i in range(1, 7)]
PRIMARY = "postgres-primary"
CACHE_MISS = 0.12  # catalog-api's normal share of cache misses

FLAGS = {
    "recs.new_ranker": ("0%", "Share of users whose home rows come from ranker v2."),
    "billing.payments_fallback": (
        "off",
        "When on, checkout accepts the order and charges the card later, once Tollpine answers again.",
    ),
    "search.circuit_breaker": (
        "off",
        "When on, search-api stops calling search-index for 30 s after repeated failures and answers from its cache.",
    ),
    "catalog.request_coalescing": (
        "off",
        "When on, catalog-api sends one database query per missing cache key; other requests for that key wait for it.",
    ),
    "playback.prefetch_next_episode": ("on", "Start buffering the next episode near the end of the current one."),
}

VOLUMES = {"pg-data": ("postgres-primary", 2000), "search-data": ("search-index", 600)}


@dataclass
class Topology:
    seed: int
    components: dict[str, Component]
    routes: list[Route]
    pods: dict[str, dict[str, str]]  # service -> {pod name: node}; rebuilt by the world when versions change
    placement: dict[str, list[str]]  # service -> the nodes its pods run on
    history: dict[str, list[tuple[str, str]]]  # component -> [(date, what changed)]

    def callers(self, target: str) -> list[str]:
        return [c.name for c in self.components.values() if any(d.target == target for d in c.deps)]

    def order(self) -> list[str]:
        """Leaves first: every component comes after everything it calls."""
        seen: list[str] = []

        def visit(name: str) -> None:
            if name in seen:
                return
            for d in self.components[name].deps:
                visit(d.target)
            seen.append(name)

        for name in self.components:
            visit(name)
        return seen

    def services(self) -> list[str]:
        return [c.name for c in self.components.values() if c.kind == "service"]

    def dep(self, caller: str, target: str) -> Dep | None:
        return next((d for d in self.components[caller].deps if d.target == target), None)


def diurnal(t: float) -> float:
    """Traffic as a share of the evening peak (20:00 UTC), from 0.25 at 08:00 to 1.0."""
    h = (t / 3600) % 24
    return 0.625 + 0.375 * math.cos(2 * math.pi * (h - 20) / 24)


def build_topology(seed: int) -> Topology:
    r = rng_for(seed, "topology")
    reads = "postgres-replica" if r.random() < 0.7 else PRIMARY
    recs_calls_catalog = r.random() < 0.7
    search_calls_catalog = r.random() < 0.5
    playback_calls_recs = r.random() < 0.4

    def svc(name, base_ms, deps, replicas=3, cpu=35.0, memory=48.0, base_err=0.002, logs=True):
        return Component(name, "service", OWNERS[name], SERVICE_ABOUT[name], base_ms, deps, replicas, cpu, memory,
                         base_err, logs)  # fmt: skip

    def store(name, base_ms, cpu=30.0):
        return Component(name, "datastore", OWNERS[name], SERVICE_ABOUT[name], base_ms, [], 0, cpu, 55.0, 0.0, False)

    def vendor(name, base_ms):
        return Component(name, "vendor", OWNERS[name], SERVICE_ABOUT[name], base_ms, [], 0, 0.0, 0.0, 0.0, False)

    components = [
        svc("gateway", 1.5, [], replicas=3, cpu=22.0, memory=30.0, base_err=0.0),
        svc("auth-api", 14, [Dep("redis", 0.9, True, 500), Dep(PRIMARY, 0.35, True)], cpu=31.0),
        svc("catalog-api", 18, [Dep("redis", 0.97, False, 300), Dep(reads, CACHE_MISS, True)], cpu=38.0),
        svc("playback-api", 22,
            [Dep("redis", 0.8, True, 500), Dep(PRIMARY, 0.25, True), Dep("catalog-api", 0.22, True, 3000)]
            + ([Dep("recommendations-api", 0.15, False, 800)] if playback_calls_recs else []), cpu=41.0),
        svc("search-api", 35, [Dep("search-index", 1.0, True, 1500)]
            + ([Dep("catalog-api", 0.3, False, 800)] if search_calls_catalog else []), cpu=36.0),
        svc("recommendations-api", 40, [Dep("redis", 0.7, False, 300)]
            + ([Dep("catalog-api", 0.5, True, 3000)] if recs_calls_catalog else []), cpu=44.0, memory=52.0),
        svc("billing-api", 30, [Dep(PRIMARY, 1.0, True), Dep("tollpine", 0.35, True, 5000),
                                Dep("events-queue", 0.4, False, 1000)], replicas=2, cpu=24.0),
        svc("notifications-worker", 60, [Dep("events-queue", 1.0, True), Dep("postwick", 0.9, True, 5000)],
            replicas=2, cpu=18.0),
        store(PRIMARY, 4.0, cpu=34.0),
        store("postgres-replica", 3.5, cpu=29.0),
        store("redis", 0.6, cpu=21.0),
        store("search-index", 22.0, cpu=33.0),
        store("events-queue", 2.0, cpu=12.0),
        vendor("tollpine", 280.0),
        vendor("postwick", 140.0),
    ]  # fmt: skip
    by_name = {c.name: c for c in components}

    placement: dict[str, list[str]] = {}
    for c in components:
        if c.kind == "service":
            nodes = NODES[:]
            rng_for(seed, "placement", c.name).shuffle(nodes)
            placement[c.name] = nodes[: c.replicas]

    history: dict[str, list[tuple[str, str]]] = {
        "billing-api": [("2025-06-03", "card payments moved from payments-core (retired) to Tollpine")],
        "search-api": [("2026-03-18", "search moved from search-v1 (retired) to search-index")],
        "recommendations-api": [("2025-11-20", "ranker v2 added behind the flag recs.new_ranker")],
        "auth-api": [("2025-09-09", "sessions moved from postgres to redis")],
        "playback-api": [("2026-01-27", "progress heartbeats write to postgres-primary in batches")],
        "notifications-worker": [("2025-04-14", "email moved to Postwick")],
    }
    if reads == "postgres-replica":
        history["catalog-api"] = [("2026-02-10", "cache misses read from postgres-replica")]
    else:
        history["catalog-api"] = [("2024-08-05", "cache misses read from postgres-primary")]
    return Topology(seed, by_name, ROUTES, {}, placement, history)


# ── Runbooks ───────────────────────────────────────────────────────────────────

# What each team wrote down for its on-call. `mentions` are the components a runbook talks
# about; `first_action` is the step it starts with, for a workflow that follows it to the letter.
RUNBOOKS = {
    "gateway": {
        "id": "RB-01", "title": "gateway: 5xx error rate above 2%", "owner": "platform-team", "updated": "2026-05-04",
        "mentions": ["gateway"], "first_action": "escalate",
        "steps": [
            "The gateway only forwards requests: find which service answers the failing routes "
            "(upstream= in the access log) and open that service's runbook.",
            "If every service fails at once, page the platform lead.",
        ],
    },
    "auth-api": {
        "id": "RB-12", "title": "auth-api: sign-in or token errors", "owner": "identity-team", "updated": "2026-04-22",
        "mentions": ["auth-api", "redis", "postgres-primary"], "first_action": "rollback_latest",
        "steps": [
            "If auth-api was deployed in the last 24 hours, roll it back to the version before.",
            "Sessions live in redis since 2025-09: check its latency before blaming the database.",
            "Never restart postgres-primary: page the DBA team instead.",
        ],
    },
    "catalog-api": {
        "id": "RB-21", "title": "catalog-api: errors or slow pages", "owner": "catalog-team", "updated": "2026-06-30",
        "mentions": ["catalog-api", "redis", "postgres-replica", "postgres-primary"],
        "first_action": "rollback_latest",
        "steps": [
            "If catalog-api was deployed in the last 24 hours, roll it back to the version before.",
            "Cache misses hit the database: a cold cache shows up as database load first.",
        ],
    },
    "playback-api": {
        "id": "RB-30", "title": "playback-api: streams fail to start", "owner": "playback-team", "updated": "2026-07-15",
        "mentions": ["playback-api", "redis", "postgres-primary", "catalog-api"], "first_action": "rollback_latest",
        "steps": [
            "If playback-api was deployed in the last 24 hours, roll it back to the version before.",
            "Heartbeat errors alone don't stop streams; start errors do.",
        ],
    },
    "search-api": {
        "id": "RB-41", "title": "search-api: search errors", "owner": "search-team", "updated": "2025-10-02",
        "mentions": ["search-api", "search-v1"], "first_action": "restart",
        "steps": [
            "Restart search-api: it holds stale connections to search-v1 after a cluster blip.",
            "If errors continue, page the search team.",
        ],
    },
    "recommendations-api": {
        "id": "RB-50", "title": "recommendations-api: home rows fail", "owner": "recs-team", "updated": "2026-03-02",
        "mentions": ["recommendations-api", "redis", "catalog-api"], "first_action": "rollback_latest",
        "steps": [
            "If recommendations-api was deployed in the last 24 hours, roll it back to the version before.",
            "The apps hide a failed row, so errors here look smaller to customers than they are.",
        ],
    },
    "billing-api": {
        "id": "RB-60", "title": "billing-api: checkout failures", "owner": "payments-team", "updated": "2024-03-11",
        "mentions": ["billing-api", "payments-core", "postgres-primary"], "first_action": "rollback_latest",
        "steps": [
            "Checkout failures are nearly always a bad billing-api release: roll it back to the version before.",
            "If payments-core shows errors, restart it.",
        ],
    },
    "notifications-worker": {
        "id": "RB-70", "title": "notifications-worker: emails delayed", "owner": "messaging-team",
        "updated": "2025-08-19", "mentions": ["notifications-worker", "events-queue", "postwick"],
        "first_action": "restart",
        "steps": ["Restart notifications-worker if the queue lag keeps growing.", "Check Postwick's status page."],
    },
    "postgres-primary": {
        "id": "RB-80", "title": "postgres-primary: errors or saturation", "owner": "dba-team", "updated": "2026-08-01",
        "mentions": ["postgres-primary", "postgres-replica"], "first_action": "escalate",
        "steps": [
            "Never restart postgres-primary: a restart drops every connection and can take minutes to recover.",
            "Find what holds the connections (pg_stat_activity by user) and stop that client.",
            "Page the DBA team for anything else.",
        ],
    },
}  # fmt: skip

RETIRED = {"payments-core": "2025-06-03", "search-v1": "2026-03-18"}
