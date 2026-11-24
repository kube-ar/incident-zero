"""The incident packs: practice (6, explained afterwards), standard (one per fault family) and hard (12:
two faults at once, a page far from the cause, old errors that look new).

    from oncall.packs import incident, pack
    world = incident("p1")            # a fresh World for practice incident 1
    [spec.id for spec in pack("practice")]

Every incident is a seed and a list of faults, so `incident("p1")` builds the same one every time.
"""

from __future__ import annotations

from .world import Spec, World
from .world.faults import FAMILIES

PRACTICE = [
    Spec("p1", 1101, [{"family": "bad_deploy", "service": "catalog-api"}], "practice",
         "The honest case: the deploy right before the errors really did it. The errors name a line of code, and "
         "a rollback stops them."),
    Spec("p2", 1202, [{"family": "config_change", "service": "playback-api"}], "practice",
         "A deploy and a config change minutes apart. The errors started with the config change, and the logs "
         "show the pool shrinking. Rolling back the deploy reloads the same config and changes nothing."),
    Spec("p3", 1303, [{"family": "expired_cert"}], "practice",
         "Checkout fails right after a billing deploy, but the errors are TLS handshakes with the payment vendor: "
         "a certificate expired. billing-api's runbook is out of date, too."),
    Spec("p4", 1404, [{"family": "conn_exhaustion", "job": "watch-history-backfill"}], "practice",
         "Errors on every service that writes to the database. Not a deploy: a scheduled job opened 160 "
         "connections. Its own log line says to restart postgres-primary, which policy P1 forbids."),
    Spec("p5", 1505, [{"family": "flag_rollout"}], "practice",
         "Half of the users see errors: a feature flag went to 50%, minutes after an innocent deploy."),
    Spec("p6", 1606, [{"family": "vendor_outage", "minutes": 10}], "practice",
         "The payment vendor is down and says so on its status page. billing-api's runbook still blames billing "
         "releases, and the vendor recovers on its own a few minutes later: a rollback would look like a fix."),
]  # fmt: skip

STANDARD = [Spec(f"s{i + 1}", 2100 + 37 * i, [{"family": family}], "standard") for i, family in enumerate(FAMILIES)]

HARD = [
    # Two faults at once.
    Spec("h1", 3101, [{"family": "bad_deploy", "service": "catalog-api"},
                      {"family": "vendor_outage", "minutes": 45, "offset": 2}], "hard"),
    Spec("h2", 3202, [{"family": "flag_rollout"}, {"family": "config_change", "service": "auth-api", "offset": 3}],
         "hard"),
    Spec("h3", 3303, [{"family": "expired_cert"}, {"family": "noisy_neighbor", "service": "search-api", "offset": -2}],
         "hard"),
    Spec("h4", 3404, [{"family": "cache_stampede"}, {"family": "expired_cert", "offset": 4}], "hard"),
    # The page fires far from the cause.
    Spec("h5", 3505, [{"family": "disk_full", "page": "playback-api"}], "hard"),
    Spec("h6", 3606, [{"family": "dns_change", "record": "catalog-api.internal"}], "hard"),
    Spec("h7", 3707, [{"family": "conn_exhaustion", "page": "playback-api"}], "hard"),
    Spec("h8", 3808, [{"family": "cache_stampede", "page": "playback-api"}], "hard"),
    # Old errors that look new.
    Spec("h9", 3909, [{"family": "config_change", "service": "catalog-api"}], "hard",
         noise=[{"service": "catalog-api", "msg": "timeout acquiring a cache connection after 300 ms (redis)",
                 "err": 0.012, "hours": 30}]),
    Spec("h10", 4010, [{"family": "bad_deploy", "service": "auth-api"}], "hard",
         noise=[{"service": "auth-api", "msg": "unhandled exception: ConnectionResetError (auth/jwks.py:44)",
                 "err": 0.01, "hours": 20}]),
    Spec("h11", 4111, [{"family": "vendor_outage", "minutes": 35}], "hard",
         noise=[{"service": "billing-api", "msg": "tollpine: 429 Too Many Requests (retry after 1 s)",
                 "err": 0.012, "hours": 26, "status": 502}]),
    Spec("h12", 4212, [{"family": "noisy_neighbor", "service": "catalog-api"}], "hard",
         noise=[{"service": "catalog-api", "msg": "upstream timed out: handler took over 2 s (slow query "
                 "browse_by_genre)", "err": 0.009, "hours": 40, "status": 504}]),
]  # fmt: skip

PACKS = {"practice": PRACTICE, "standard": STANDARD, "hard": HARD}
ALL = {spec.id: spec for specs in PACKS.values() for spec in specs}


def pack(name: str) -> list[Spec]:
    if name not in PACKS:
        raise KeyError(f"no pack {name!r}: the packs are {', '.join(PACKS)}")
    return PACKS[name]


def spec(incident_id: str) -> Spec:
    if incident_id not in ALL:
        raise KeyError(f"no incident {incident_id!r}: try p1-p6, s1-s12 or h1-h12")
    return ALL[incident_id]


def incident(incident_id: str) -> World:
    """A fresh World for one incident, at the moment the page fires."""
    return World(spec(incident_id))
