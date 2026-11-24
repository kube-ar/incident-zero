"""How each source writes its log lines: nginx, JSON app logs, PostgreSQL, Kubernetes events, redis, search-index."""

from __future__ import annotations

import json
import random

from .base import Clock

AGENTS = [
    "Reelvane/7.14.2 (iPhone; iOS 18.6)",
    "Reelvane/7.14.1 (Android 15; Pixel 8)",
    "Reelvane/7.13.0 (Android 14; SM-S918B)",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "ReelvaneTV/3.9.0 (Tizen 8.0; Samsung)",
    "ReelvaneTV/3.8.4 (webOS 24; LG)",
]
GENRES = ["drama", "comedy", "documentary", "thriller", "animation", "sci-fi", "kids", "romance"]
ROWS = ["continue-watching", "trending", "because-you-watched", "top-picks", "new-releases"]
QUERIES = ["the long night", "cooking", "space", "detective", "season 2", "nature", "anime", "true crime", "jazz",
           "ocean", "the heist", "comedy specials", "mountain", "football"]  # fmt: skip
# Client addresses come from the ranges reserved for documentation (RFC 5737).
NETS = ["192.0.2.", "198.51.100.", "203.0.113."]


def client_ip(r: random.Random) -> str:
    return r.choice(NETS) + str(r.randint(1, 254))


def concrete_path(r: random.Random, path: str) -> str:
    if "{id}" in path:
        path = path.replace("{id}", str(r.randint(10000, 99999)))
    if "{genre}" in path:
        path = path.replace("{genre}", r.choice(GENRES)) + (f"?page={r.randint(2, 6)}" if r.random() < 0.3 else "")
    if "{row}" in path:
        path = path.replace("{row}", r.choice(ROWS))
    if path == "/v1/search":
        path += "?q=" + r.choice(QUERIES).replace(" ", "+")
    return path


def nginx(clock: Clock, t: float, r: random.Random, method: str, path: str, status: int, rt_ms: float,
          upstream: str, request_id: str) -> str:  # fmt: skip
    d = clock.at(t)
    size = r.randint(180, 900) if status >= 400 else r.randint(600, 14000)
    rt = f"{rt_ms / 1000:.3f}"
    urt = f"{max(rt_ms - 1, 0) / 1000:.3f}" if status != 504 else "-"
    return (
        f'{client_ip(r)} - - [{d.strftime("%d/%b/%Y:%H:%M:%S")} +0000] "{method} {path} HTTP/1.1" {status} {size} '
        f'"-" "{r.choice(AGENTS)}" rt={rt} urt={urt} upstream={upstream} request_id={request_id}'
    )


def app(clock: Clock, t: float, service: str, version: str, pod: str, level: str, msg: str, **fields) -> str:
    line = {"ts": clock.iso(t), "level": level, "service": service, "version": version, "pod": pod}
    line.update(fields)
    line["msg"] = msg
    return json.dumps(line, ensure_ascii=False, separators=(",", ":"))


def postgres(clock: Clock, t: float, pid: int, level: str, msg: str) -> str:
    d = clock.at(t)
    return f"{d.strftime('%Y-%m-%d %H:%M:%S')}.{d.microsecond // 1000:03d} UTC [{pid}] {level}:  {msg}"


def kube(clock: Clock, t: float, kind: str, reason: str, obj: str, msg: str) -> str:
    return f"{clock.iso(t, ms=False)} {kind:<7} {reason:<22} {obj}  {msg}"


def redis(clock: Clock, t: float, msg: str, mark: str = "*") -> str:
    d = clock.at(t)
    return f"1:M {d.strftime('%d %b %Y %H:%M:%S')}.{d.microsecond // 1000:03d} {mark} {msg}"


def search_index(clock: Clock, t: float, level: str, logger: str, node: str, msg: str) -> str:
    d = clock.at(t)
    return f"[{d.strftime('%Y-%m-%dT%H:%M:%S')},{d.microsecond // 1000:03d}][{level:<5}][{logger}] [{node}] {msg}"
