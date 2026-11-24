"""Actions on production: what each one checks, when it takes effect, and what it leaves in the logs.

Every action is also a change: it shows up in list_changes like anyone else's, by "on-call".
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from . import text
from .base import Act, Event
from .topology import FLAGS, NODES, PRIMARY, VOLUMES

if TYPE_CHECKING:
    from .sim import World

# How long each action takes to be in place, in seconds.
EFFECT = {
    "rollback": 120,
    "restart": 60,
    "scale": 90,
    "set_flag": 15,
    "revert_config": 30,
    "renew_cert": 60,
    "drain_node": 120,
    "failover": 60,
    "pause_job": 30,
    "expand_volume": 90,
    "post_status_update": 0,
}


class ActionError(ValueError):
    pass


def _one_of(value, options, what: str) -> str:
    value = str(value or "").strip()
    if value not in options:
        raise ActionError(f"no {what} {value!r}: the {what}s are {', '.join(options)}")
    return value


def normalize_flag(flag: str, value) -> str:
    current = FLAGS[flag][0]
    raw = str(value).strip().lower()
    if current.endswith("%"):
        number = raw.rstrip("%").strip()
        if raw in ("off", "false"):
            number = "0"
        if not number.replace(".", "", 1).isdigit() or not 0 <= float(number) <= 100:
            raise ActionError(f"{flag} is a percentage of users: set it to a value like 0% or 25%")
        return f"{float(number):g}%"
    if raw in ("on", "true", "1", "yes", "enabled"):
        return "on"
    if raw in ("off", "false", "0", "no", "disabled"):
        return "off"
    raise ActionError(f"{flag} is on or off")


def apply_action(w: World, n: int, t: float, tool: str, args: dict) -> tuple[str, Act]:
    """Check the arguments, apply the action, and say what happened. Raises ActionError for a bad call."""
    args = dict(args)
    services = [s for s in w.topo.services()]
    clock = w.clock
    effect = t + EFFECT[tool]
    events: list[Event] = []
    change_kind, target, summary, meta = tool, "", "", {}

    if tool == "rollback":
        service = _one_of(args.get("service"), services, "service")
        version = str(args.get("version") or "").strip().lstrip("v")
        ran = [v for when, v, _ in w.versions[service] if when <= t]
        current = w.version(service, t)
        if version not in ran:
            raise ActionError(f"{service} never ran version {version!r}; versions it ran: {', '.join(ran[-5:])}")
        if version == current:
            raise ActionError(f"{service} already runs {version}")
        args = {"service": service, "version": version}
        target, summary = service, f"Roll back {service} {current} -> {version}"
        meta = {"version": version, "previous_version": current}
        done = (
            f"Rolling back {service} from {current} to {version}: pods are replaced one at a time, done by about "
            f"{clock.hm(effect)}."
        )
        events.append(Event(t + 2, "kube-events", text.kube(clock, t + 2, "Normal", "ScalingReplicaSet",
                            f"deployment/{service}", f"Rollback to {version} started by on-call")))  # fmt: skip
    elif tool == "restart":
        components = services + ["postgres-primary", "postgres-replica", "redis", "search-index", "events-queue"]
        component = _one_of(args.get("component"), components, "component")
        args = {"component": component}
        target, summary = component, f"Restart {component}"
        done = f"Restarting {component}: back by about {clock.hm(effect)}."
        if component.startswith("postgres"):
            events.append(Event(t + 1, component, text.postgres(clock, t + 1, 1, "LOG",
                                "received fast shutdown request")))  # fmt: skip
            events.append(Event(effect, component, text.postgres(clock, effect, 1, "LOG",
                                "database system is ready to accept connections")))  # fmt: skip
        else:
            events.append(Event(t + 2, "kube-events", text.kube(clock, t + 2, "Normal", "Killing",
                                f"deployment/{component}", "Restart requested by on-call")))  # fmt: skip
    elif tool == "scale":
        service = _one_of(args.get("service"), services, "service")
        try:
            replicas = int(args.get("replicas"))
        except (TypeError, ValueError):
            raise ActionError("replicas must be a whole number from 1 to 12") from None
        if not 1 <= replicas <= 12:
            raise ActionError("replicas must be a whole number from 1 to 12")
        args = {"service": service, "replicas": replicas}
        target, summary = service, f"Scale {service} to {replicas} replicas"
        done = f"Scaling {service} to {replicas} replicas: ready by about {clock.hm(effect)}."
        events.append(Event(t + 2, "kube-events", text.kube(clock, t + 2, "Normal", "ScalingReplicaSet",
                            f"deployment/{service}", f"Scaled to {replicas} by on-call")))  # fmt: skip
    elif tool == "set_flag":
        flag = _one_of(args.get("flag"), list(FLAGS), "flag")
        value = normalize_flag(flag, args.get("value"))
        old = w.flag(flag, t)
        if value == old:
            raise ActionError(f"{flag} is already {value}")
        args = {"flag": flag, "value": value}
        w.flags[flag].append((effect, value, None))
        target, summary, change_kind = flag, f"Set {flag} {old} -> {value}", "flag"
        done = f"{flag} is {value} from {clock.hms(effect)}."
    elif tool == "revert_config":
        cid = str(args.get("change_id") or "").strip()
        change = next((c for c in w.changes if c.id == cid), None)
        if change is None or change.kind not in ("config", "dns") or change.by_oncall:
            ids = [c.id for c in w.changes if c.kind in ("config", "dns") and not c.by_oncall and c.t <= t]
            raise ActionError(f"{cid!r} isn't a config or DNS change; those are: {', '.join(ids) or 'none'}")
        if any(a.tool == "revert_config" and a.args.get("change_id") == cid for a in w.acts):
            raise ActionError(f"{cid} was already reverted")
        args = {"change_id": cid}
        if change.kind == "dns":
            w.dns[change.target].append((effect + 30, change.meta["old"], None))
            done = (
                f"{change.target} points back at {change.meta['old']}; clients pick it up within a minute (TTL 60 s)."
            )
        elif "key" in change.meta and (change.target, change.meta["key"]) in w.config:
            w.config[(change.target, change.meta["key"])].append((effect, change.meta["old"], None))
            done = (
                f"{change.target}: {change.meta['key']} back to {change.meta['old']}, reloaded live by about "
                f"{clock.hms(effect)}."
            )
            for pod, _ in w.pods(change.target, effect):
                events.append(Event(effect, change.target, text.app(clock, effect, change.target,
                                    w.version(change.target, effect), pod, "info", "config reloaded",
                                    key=change.meta["key"], old=change.meta["new"], new=change.meta["old"])))  # fmt: skip
        else:
            done = f"Reverted {cid}."
        target, summary, change_kind = change.target, f"Revert {cid}", change.kind
    elif tool == "renew_cert":
        cert = _one_of(args.get("cert"), list(w.certs), "certificate")
        args = {"cert": cert}
        w.certs[cert]["not_after"].append((effect, effect + 90 * 86400))
        target, summary, change_kind = cert, f"Renew certificate {cert}", "cert"
        done = f"New {cert} certificate issued and loaded by about {clock.hm(effect)}, valid 90 days."
        if cert == "tollpine-client":
            for pod, _ in w.pods("billing-api", effect):
                events.append(Event(effect, "billing-api", text.app(clock, effect, "billing-api",
                                    w.version("billing-api", effect), pod, "info", "client certificate reloaded",
                                    cert=cert, not_after=clock.iso(effect + 90 * 86400, ms=False))))  # fmt: skip
    elif tool == "drain_node":
        node = _one_of(args.get("node"), NODES, "node")
        if w.drained(node, math.inf):
            raise ActionError(f"{node} is already drained")
        args = {"node": node}
        target, summary, change_kind = node, f"Drain {node}", "drain"
        done = f"Draining {node}: its pods move to other nodes, done by about {clock.hm(effect)}."
        events.append(Event(t + 3, "kube-events", text.kube(clock, t + 3, "Normal", "NodeNotSchedulable",
                            f"node/{node}", "Node cordoned and draining (on-call)")))  # fmt: skip
    elif tool == "failover":
        database = _one_of(args.get("database"), [PRIMARY], "database")
        args = {"database": database}
        target, summary = database, "Fail postgres-primary over to postgres-replica"
        done = "Promoting postgres-replica: writes are refused for about a minute while clients reconnect."
        events.append(Event(effect - 50, PRIMARY, text.postgres(clock, effect - 50, 1, "LOG",
                            "received promote request; terminating connections")))  # fmt: skip
    elif tool == "pause_job":
        job = _one_of(args.get("job"), list(w.jobs), "job")
        if w.job_paused(job, math.inf):
            raise ActionError(f"{job} is already paused")
        args = {"job": job}
        target, summary, change_kind = job, f"Pause job {job}", "job"
        done = f"{job} suspended: its running pods stop by about {clock.hms(effect)}."
        events.append(Event(t + 2, "kube-events", text.kube(clock, t + 2, "Normal", "SuspendedJob", f"cronjob/{job}",
                            "Suspended by on-call")))  # fmt: skip
    elif tool == "expand_volume":
        volume = _one_of(args.get("volume"), list(VOLUMES), "volume")
        current = w.volume_size(volume, t)
        try:
            size = int(args.get("size_gb"))
        except (TypeError, ValueError):
            raise ActionError(f"size_gb must be a whole number of GB, bigger than today's {current:g}") from None
        if size <= current or size > current * 3:
            raise ActionError(f"size_gb must be bigger than today's {current:g} GB and at most {current * 3:g}")
        args = {"volume": volume, "size_gb": size}
        w.volumes[volume]["size_gb"].append((effect, size))
        target, summary, change_kind = volume, f"Expand {volume} {current:g} GB -> {size} GB", "volume"
        done = f"{volume} grows to {size} GB online, by about {clock.hm(effect)}."
    elif tool == "post_status_update":
        message = str(args.get("text") or "").strip()
        if len(message) < 15:
            raise ActionError("write the update customers will read: what's affected, and that you're on it")
        args = {"text": message[:500]}
        w.status_updates.append((t, args["text"]))
        act = Act(n, t, tool, args, t)
        w.acts.append(act)
        return f"Posted on status.reelvane.example at {clock.hm(t)}.", act
    else:  # pragma: no cover
        raise ActionError(f"no action {tool!r}")

    change = w.add_change(t, change_kind, target, summary, "on-call", "", **meta)
    change.id, change.by_oncall = w.new_change_id(), True
    if tool == "rollback":
        change.version, change.previous_version = meta["version"], meta["previous_version"]
        w.versions[args["service"]].append((effect, meta["version"], change.id))
        w.versions[args["service"]].sort(key=lambda e: e[0])
    act = Act(n, t, tool, args, effect, change.id)
    w.acts.append(act)
    for e in events:
        w.schedule(e)
    return f"{done} ({change.id})", act
