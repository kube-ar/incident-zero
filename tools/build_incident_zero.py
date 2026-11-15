"""Writes incidents/zero, the fixed incident of milestone 0. Maintainers only: learners never run it.

    uv run python tools/build_incident_zero.py            # rewrites incidents/zero
    uv run python tools/build_incident_zero.py --out DIR  # writes somewhere else (the tests compare)

Everything comes from one seed, so a second run writes the same files byte for byte. Change the
story here, never by hand in the files: truth.json is computed from the lines this script writes.

The story (spoilers): Reelvane's catalog-api 2.41.0 rolls out at 14:02 and only renames a row
title. At 14:05:48 a config change meant to trim idle database connections lowers
`max_connections` from 40 to 4 instead, and the service reloads it live. From then on most
catalog requests wait 2 s for a connection and fail with a 500. The support lead blames the
deployment, because it was the last one.
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

SEED = 20261112
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "incidents" / "zero"


def at(hms: str) -> datetime:
    h, m, s = hms.split(":")
    sec = float(s)
    return datetime(2026, 11, 12, int(h), int(m), int(sec), int(round((sec % 1) * 1e6)), tzinfo=UTC)


START, END = at("13:50:00"), at("14:12:00")
# The rolling deploy of catalog-api 2.41.0 replaces one pod slot at a time; the last is ready at 14:02:31.
SLOT_READY = [at("14:02:13"), at("14:02:22"), at("14:02:31")]
CONFIG_AT = at("14:05:48.512")
ALERT_AT = at("14:08:00")
MEAN_GAP_S = 3.6  # the logs keep one request in 500, the same ones on every service (by request id)

CHANGES = [
    {
        "id": "CHG-2044",
        "at": "2026-11-12T14:05:48Z",
        "kind": "config",
        "service": "catalog-api",
        "author": "tomas",
        "summary": "Trim idle database connections on catalog-api (OPS-2291)",
    },
    {
        "id": "CHG-2043",
        "at": "2026-11-12T14:02:31Z",
        "kind": "deploy",
        "service": "catalog-api",
        "version": "2.41.0",
        "previous_version": "2.40.3",
        "author": "lena",
        "summary": "Rename the 'Continue watching' row to 'Pick up where you left off'",
    },
    {
        "id": "CHG-2042",
        "at": "2026-11-12T13:21:55Z",
        "kind": "deploy",
        "service": "recommendations",
        "version": "1.9.2",
        "previous_version": "1.9.1",
        "author": "amira",
        "summary": "Cache 'Because you watched' rows for 10 minutes",
    },
    {
        "id": "CHG-2041",
        "at": "2026-11-12T09:14:02Z",
        "kind": "deploy",
        "service": "playback-api",
        "version": "3.18.0",
        "previous_version": "3.17.4",
        "author": "joon",
        "summary": "Add Brazilian Portuguese to the subtitle picker",
    },
]

DIFFS = {
    "CHG-2044": """\
config-service · catalog-api/production.yaml
Applied live (hot reload) 2026-11-12 14:05:48 UTC by tomas
Ticket OPS-2291: "catalog-api keeps about 40 idle connections open to catalog-db all night"

--- a/catalog-api/production.yaml
+++ b/catalog-api/production.yaml
@@ -6,9 +6,9 @@ database:
   host: catalog-db-primary.internal
   port: 5432
   name: catalog
-  max_connections: 40
+  max_connections: 4
   min_idle: 10
   idle_timeout_s: 600
   acquire_timeout_ms: 2000
""",
    "CHG-2043": """\
commit 7f3e9a1c4b2d8e6f0a5c9b3d1e7f2a4c6b8d0e1f
Author: lena
Date:   2026-11-12 13:41 UTC
Deployed: catalog-api 2.40.3 -> 2.41.0, rolling, finished 2026-11-12 14:02:31 UTC

    Rename the 'Continue watching' row to 'Pick up where you left off'

    Product asked for a warmer label (UX-418). Copy change only.

diff --git a/locales/en.json b/locales/en.json
@@ -12,7 +12,7 @@
   "rows.trending": "Trending now",
-  "rows.continue_watching": "Continue watching",
+  "rows.continue_watching": "Pick up where you left off",
   "rows.because_you_watched": "Because you watched {title}",
diff --git a/locales/es.json b/locales/es.json
@@ -12,7 +12,7 @@
   "rows.trending": "Tendencias",
-  "rows.continue_watching": "Seguir viendo",
+  "rows.continue_watching": "Retoma donde lo dejaste",
   "rows.because_you_watched": "Porque viste {title}",
diff --git a/tests/snapshots/home_rows.json b/tests/snapshots/home_rows.json
@@ -3,7 +3,7 @@
     {"id": "trending", "title": "Trending now"},
-    {"id": "continue_watching", "title": "Continue watching"},
+    {"id": "continue_watching", "title": "Pick up where you left off"},
     {"id": "because_you_watched", "title": "Because you watched Night Shift"},
""",
    "CHG-2042": """\
commit 2b8d4f6a0c1e3a5b7d9f1e3c5a7b9d0f2e4a6c8b
Author: amira
Date:   2026-11-12 12:55 UTC
Deployed: recommendations 1.9.1 -> 1.9.2, rolling, finished 2026-11-12 13:21:55 UTC

    Cache 'Because you watched' rows for 10 minutes

    The row is rebuilt on every home screen load; viewers rarely finish a title in 10 minutes.

diff --git a/src/rows/because_you_watched.py b/src/rows/because_you_watched.py
@@ -1,6 +1,8 @@
+from functools import partial
+from ..cache import cached

-def because_you_watched(profile_id: str, title_id: str) -> list[str]:
+@partial(cached, ttl_s=600)
+def because_you_watched(profile_id: str, title_id: str) -> list[str]:
     neighbours = similar_titles(title_id, k=40)
     return rank_for_profile(profile_id, neighbours)[:12]
""",
    "CHG-2041": """\
commit 9c1a3e5b7d0f2a4c6e8b1d3f5a7c9e0b2d4f6a8c
Author: joon
Date:   2026-11-12 08:50 UTC
Deployed: playback-api 3.17.4 -> 3.18.0, rolling, finished 2026-11-12 09:14:02 UTC

    Add Brazilian Portuguese to the subtitle picker

diff --git a/src/subtitles/languages.py b/src/subtitles/languages.py
@@ -9,6 +9,7 @@ LANGUAGES = {
     "fr": "Français",
     "it": "Italiano",
     "pt": "Português",
+    "pt-BR": "Português (Brasil)",
     "es": "Español",
     "es-419": "Español (Latinoamérica)",
 }
""",
}

AGENTS = [
    "Reelvane/7.12.0 (Android 15; Pixel 8)",
    "Reelvane/7.12.0 (Android 14; SM-S918B)",
    "Reelvane/7.12.0 (iOS 18.1; iPhone15,2)",
    "Reelvane/7.11.2 (iOS 17.6; iPhone13,2)",
    "Reelvane-TV/4.3.1 (tvOS 18.1; AppleTV14,1)",
    "Reelvane-TV/4.3.0 (Android TV 12; BRAVIA 4K)",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
]
NETS = ["192.0.2", "198.51.100", "203.0.113"]  # reserved for documentation: no real client
GENRES = ["drama", "comedy", "documentary", "thriller", "kids", "anime", "sci-fi"]


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def nginx_time(t: datetime) -> str:
    return t.strftime("%d/%b/%Y:%H:%M:%S +0000")


def pod_name(rng: random.Random, deployment: str, template: str) -> str:
    return f"{deployment}-{template}-" + "".join(rng.choice("bcdfghjklmnpqrstvwxz2456789") for _ in range(5))


def jline(**fields) -> str:
    return json.dumps(fields, ensure_ascii=False, separators=(",", ":"))


def build(rng: random.Random) -> dict:
    old_pods = [pod_name(rng, "catalog-api", "5b7d9c6f8") for _ in range(3)]
    new_pods = [pod_name(rng, "catalog-api", "6c4f8d7b9") for _ in range(3)]
    play_pods = [pod_name(rng, "playback-api", "7d9f6b5c4") for _ in range(2)]

    gateway: list[tuple[datetime, str]] = []
    catalog: list[tuple[datetime, str, str]] = []  # (time, line, kind)
    playback: list[tuple[datetime, str]] = []

    def catalog_pod(t: datetime) -> tuple[str, str]:
        slot = rng.randrange(3)
        return (new_pods[slot], "2.41.0") if t >= SLOT_READY[slot] else (old_pods[slot], "2.40.3")

    # The rolling deploy, one slot at a time: the new pod starts, opens its pool and is ready; the old one drains.
    for slot, ready in enumerate(SLOT_READY):
        new, old = new_pods[slot], old_pods[slot]
        base = {"service": "catalog-api", "version": "2.41.0", "pod": new}
        catalog.append((ready - timedelta(seconds=8.4), jline(ts=iso(ready - timedelta(seconds=8.4)), level="info", **base,
                        msg="starting catalog-api", commit="7f3e9a1"), "lifecycle"))  # fmt: skip
        t = ready - timedelta(seconds=3.1)
        catalog.append((t, jline(ts=iso(t), level="info", **base, msg="database pool ready", host="catalog-db-primary.internal",
                        max_connections=40, min_idle=10), "pool ready"))  # fmt: skip
        catalog.append((ready, jline(ts=iso(ready), level="info", **base, msg="ready", listen=":8080"), "lifecycle"))
        t = ready + timedelta(seconds=0.2)
        catalog.append((t, jline(ts=iso(t), level="info", service="catalog-api", version="2.40.3", pod=old,
                                 msg="received SIGTERM, draining connections"), "lifecycle"))  # fmt: skip
        t = ready + timedelta(seconds=2.6)
        catalog.append((t, jline(ts=iso(t), level="info", service="catalog-api", version="2.40.3", pod=old,
                                 msg="shut down cleanly", served_since_start=rng.randrange(180_000, 260_000)), "lifecycle"))  # fmt: skip

    # The config change reaches every pod within a second; each one resizes its pool.
    for i, pod in enumerate(new_pods):
        t = CONFIG_AT + timedelta(milliseconds=170 * i)
        base = {"service": "catalog-api", "version": "2.41.0", "pod": pod}
        catalog.append((t, jline(ts=iso(t), level="info", **base, msg="config reloaded", source="config-service",
                                 changed={"database.max_connections": {"from": 40, "to": 4}}), "config reload"))  # fmt: skip
        t2 = t + timedelta(milliseconds=rng.randrange(8, 30))
        catalog.append((t2, jline(ts=iso(t2), level="warn", **base, msg="database pool resized", max_connections=4,
                                  min_idle=10, closing_connections=rng.randrange(28, 37)), "pool resized"))  # fmt: skip

    # A saturation monitor logs only when requests are waiting for a connection: every 30 s per pod, after the change.
    for i, pod in enumerate(new_pods):
        t = CONFIG_AT + timedelta(seconds=25 + 7 * i)
        while t < END:
            catalog.append((t, jline(ts=iso(t), level="warn", service="catalog-api", version="2.41.0", pod=pod,
                                     msg="database pool saturated", max_connections=4, in_use=4,
                                     waiting=rng.randrange(19, 44)), "pool saturated"))  # fmt: skip
            t += timedelta(seconds=30)

    t = START
    while True:
        t += timedelta(seconds=rng.expovariate(1 / MEAN_GAP_S))
        if t >= END:
            break
        rid = f"{rng.getrandbits(128):032x}"
        ip = f"{rng.choice(NETS)}.{rng.randrange(2, 254)}"
        agent = rng.choice(AGENTS)
        kind = rng.choices(["catalog", "playback", "auth", "recommendations"], weights=[58, 28, 9, 5])[0]
        app_line, status = None, 200

        if kind == "catalog":
            route, path = rng.choices(
                [("/v1/home", "/v1/home"), ("/v1/rows/continue-watching", "/v1/rows/continue-watching"),
                 ("/v1/titles/:id", f"/v1/titles/{rng.randrange(10000, 99999)}"),
                 ("/v1/browse/:genre", f"/v1/browse/{rng.choice(GENRES)}?page={rng.randrange(1, 6)}")],
                weights=[4, 2, 3, 2],
            )[0]  # fmt: skip
            pod, version = catalog_pod(t)
            broken = version == "2.41.0" and t >= CONFIG_AT + timedelta(milliseconds=600)
            level, extra, msg = "info", {}, "request completed"
            if broken and rng.random() < 0.66:
                status, duration, wait = 500, rng.randrange(2001, 2007), 2000
                level, msg = "error", "request failed"
                extra = {"error": f"timed out after 2000ms waiting for a database connection "
                                  f"(max_connections=4, in_use=4, waiting={rng.randrange(17, 41)})"}  # fmt: skip
                body = 87
            elif route == "/v1/titles/:id" and rng.random() < 0.04:
                status, duration, wait, body = 404, rng.randrange(6, 15), rng.randrange(0, 2), 64
                msg = "title not found"
            else:
                wait = rng.randrange(380, 1950) if broken else rng.randrange(0, 3)
                duration = wait + rng.randrange(14, 95)
                body = {"/v1/home": rng.randrange(18000, 26000), "/v1/rows/continue-watching": rng.randrange(4000, 9000),
                        "/v1/titles/:id": rng.randrange(3000, 6200), "/v1/browse/:genre": rng.randrange(9000, 15000)}[route]  # fmt: skip
                if route == "/v1/browse/:genre" and rng.random() < 0.18:  # an old slow query, there all day
                    duration += rng.randrange(850, 1400)
                    level, msg = "warn", "slow query"
                    extra = {"query": "browse_by_genre", "query_ms": duration - wait - rng.randrange(5, 12)}
            end = t + timedelta(milliseconds=duration)
            app_line = (end, jline(ts=iso(end), level=level, service="catalog-api", version=version, pod=pod,
                                   request_id=rid, method="GET", route=route, status=status, duration_ms=duration,
                                   db_wait_ms=wait, msg=msg, **extra))  # fmt: skip
            catalog.append((*app_line, "pool timeout" if status == 500 else "request"))
            upstream = "catalog-api"
            method = "GET"
        elif kind == "playback":
            route, path, method = rng.choices(
                [("/v1/play/heartbeat", "/v1/play/heartbeat", "POST"),
                 ("/v1/play/:id/start", f"/v1/play/{rng.randrange(10000, 99999)}/start", "POST"),
                 ("/v1/play/:id/manifest", f"/v1/play/{rng.randrange(10000, 99999)}/manifest.mpd", "GET")],
                weights=[5, 2, 2],
            )[0]  # fmt: skip
            pod = rng.choice(play_pods)
            level, msg, extra = "info", "request completed", {}
            if route == "/v1/play/heartbeat":
                status, duration, body = 204, rng.randrange(3, 12), 0
            elif route == "/v1/play/:id/start":
                status, duration, body = 200, rng.randrange(60, 180), rng.randrange(600, 900)
                if rng.random() < 0.1:  # the DRM license server is slow now and then, all day
                    duration += rng.randrange(450, 800)
                    level, msg, extra = "warn", "license server slow, retried once", {"license_ms": duration - 40}
            else:
                status, duration, body = 200, rng.randrange(20, 70), rng.randrange(2400, 3100)
            end = t + timedelta(milliseconds=duration)
            playback.append((end, jline(ts=iso(end), level=level, service="playback-api", version="3.18.0", pod=pod,
                                        request_id=rid, method=method, route=route, status=status,
                                        duration_ms=duration, msg=msg, **extra)))  # fmt: skip
            upstream = "playback-api"
        elif kind == "auth":
            path, method, upstream = "/v1/auth/refresh", "POST", "auth-api"
            duration = rng.randrange(22, 70)
            status, body = (401, 52) if rng.random() < 0.04 else (200, rng.randrange(410, 452))
            end = t + timedelta(milliseconds=duration)
        else:
            path, method, upstream = (
                f"/v1/rows/because-you-watched?title={rng.randrange(10000, 99999)}",
                "GET",
                "recommendations",
            )
            duration = rng.randrange(8, 40)
            status, body = 200, rng.randrange(2800, 4200)
            end = t + timedelta(milliseconds=duration)

        rt = duration / 1000 + rng.randrange(1, 4) / 1000
        gateway.append((end, f'{ip} - - [{nginx_time(end)}] "{method} {path} HTTP/1.1" {status} {body} "-" "{agent}" '
                             f"rt={rt:.3f} urt={duration / 1000:.3f} upstream={upstream} request_id={rid}"))  # fmt: skip

    gateway.sort(key=lambda x: x[0])
    catalog.sort(key=lambda x: x[0])
    playback.sort(key=lambda x: x[0])
    return {"gateway": gateway, "catalog": catalog, "playback": playback}


def page(gateway: list[tuple[datetime, str]]) -> tuple[str, dict]:
    window = [line for t, line in gateway if ALERT_AT - timedelta(minutes=2) <= t < ALERT_AT]
    errors = sum(line.split('" ')[1].split()[0].startswith("5") for line in window)
    rate = 100 * errors / len(window)
    first = next(t for t, line in gateway if line.split('" ')[1].split()[0].startswith("5"))
    text = f"""\
INCIDENT #0 · SEV-2 · triggered {ALERT_AT:%Y-%m-%d %H:%M} UTC · on call: you

[FIRING] gateway: 5xx error rate {rate:.1f}% over the last 2 minutes (threshold 2%)
Service: gateway (the public API in front of every Reelvane app)
First 5xx: {first:%H:%M} UTC

Message from Dana (support lead) in #incidents, {ALERT_AT + timedelta(minutes=1):%H:%M} UTC:
"Customers can't load the home screen on any app. Our API started returning 500s
right after the last deployment. Can you take a look?"
"""
    return text, {"window": len(window), "errors": errors, "rate": round(rate, 1), "first_5xx": iso(first)}


def write(out: Path) -> None:
    rng = random.Random(SEED)
    logs = build(rng)
    (out / "logs").mkdir(parents=True, exist_ok=True)
    (out / "diffs").mkdir(parents=True, exist_ok=True)

    gateway = [line for _, line in logs["gateway"]]
    catalog = [line for _, line, _ in logs["catalog"]]
    playback = [line for _, line in logs["playback"]]
    for name, lines in (("gateway", gateway), ("catalog-api", catalog), ("playback-api", playback)):
        (out / "logs" / f"{name}.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

    text, alert = page(logs["gateway"])
    (out / "page.txt").write_text(text, encoding="utf-8")
    (out / "changes.json").write_text(json.dumps(CHANGES, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for cid, diff in DIFFS.items():
        (out / "diffs" / f"{cid}.diff").write_text(diff, encoding="utf-8")

    groups: dict[str, list[str]] = {}
    for n, (_, _, kind) in enumerate(logs["catalog"], start=1):
        groups.setdefault(kind, []).append(f"catalog-api:{n}")
    symptoms = [f"gateway:{n}" for n, (t, line) in enumerate(logs["gateway"], start=1)
                if " 500 " in line and "upstream=catalog-api" in line]  # fmt: skip
    first_error = next(t for t, _, kind in logs["catalog"] if kind == "pool timeout")
    truth = {
        "incident": "zero",
        "cause": "CHG-2044",
        "trap": "CHG-2043",
        "story": "The catalog-api 2.41.0 deployment (CHG-2043) only renamed a row title, and the new version "
        "served clean traffic for three minutes. At 14:05:48 a config change meant to trim idle database "
        "connections (CHG-2044) lowered max_connections from 40 to 4 instead, and every catalog pod reloaded "
        "it live. Most catalog requests then waited 2 s for a connection and failed with a 500.",
        "support": {
            "config reload": groups["config reload"],
            "pool resized": groups["pool resized"],
            "pool timeouts": groups["pool timeout"],
            "pool saturated": groups["pool saturated"],
        },
        "against the deploy": {"pool ready with 40 connections": groups["pool ready"]},
        "symptoms": {"gateway 500s from catalog-api": symptoms},
        "deploy_finished": iso(SLOT_READY[-1]),
        "config_applied": iso(CONFIG_AT),
        "first_error": iso(first_error),
        "alert": alert,
    }
    (out / "truth.json").write_text(json.dumps(truth, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}: {len(gateway)} gateway, {len(catalog)} catalog-api, {len(playback)} playback-api lines; "
          f"alert {alert['rate']}% ({alert['errors']}/{alert['window']})")  # fmt: skip


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=OUT)
    write(parser.parse_args().out)
