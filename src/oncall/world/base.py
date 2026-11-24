"""Small pieces every part of the simulator shares: seeded randomness, time, and failure modes."""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

SAMPLE = 500  # access logs keep one request in 500; errors are logged up to ERRORS_PER_MINUTE per message
ERRORS_PER_MINUTE = 3


def rng_for(seed: int, *parts) -> random.Random:
    """A random stream of its own for each part of the world, so changing one part never shifts another.

    Python's hash() of a string changes between runs, so the stream is keyed with blake2b instead.
    """
    digest = hashlib.blake2b(repr((seed, *parts)).encode(), digest_size=8).digest()
    return random.Random(int.from_bytes(digest, "big"))


def poisson(r: random.Random, mean: float) -> int:
    if mean <= 0:
        return 0
    if mean > 30:  # normal approximation, plenty for log volumes
        return max(0, round(r.gauss(mean, math.sqrt(mean))))
    limit, k, p = math.exp(-mean), 0, 1.0
    while True:
        p *= r.random()
        if p <= limit:
            return k
        k += 1


def hexid(r: random.Random, n: int = 32) -> str:
    return f"{r.getrandbits(4 * n):0{n}x}"


@dataclass(frozen=True)
class Clock:
    """Turns the simulator's seconds (from 00:00 UTC on the incident day) into dates and back."""

    day: datetime  # 00:00 UTC

    def at(self, t: float) -> datetime:
        return self.day + timedelta(seconds=t)

    def iso(self, t: float, ms: bool = True) -> str:
        d = self.at(t)
        return d.strftime("%Y-%m-%dT%H:%M:%S") + (f".{d.microsecond // 1000:03d}Z" if ms else "Z")

    def hm(self, t: float) -> str:
        return self.at(t).strftime("%H:%M")

    def hms(self, t: float) -> str:
        return self.at(t).strftime("%H:%M:%S")

    def when(self, t: float) -> str:
        """Short and unambiguous for the model: '14:05' today, 'Nov 13 22:40' on another day."""
        d = self.at(t)
        if d.date() == self.day.date():
            return d.strftime("%H:%M")
        return d.strftime("%b %d %H:%M").replace(" 0", " ")

    def parse(self, text: str) -> float:
        """'14:05', '14:05:30' (today) or an ISO time, back to seconds."""
        text = text.strip()
        if "T" in text or "-" in text:
            d = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=UTC)
            return (d - self.day).total_seconds()
        parts = [int(p) for p in text.split(":")]
        while len(parts) < 3:
            parts.append(0)
        return parts[0] * 3600 + parts[1] * 60 + parts[2]


def hm(t: float) -> int:
    """Minutes from seconds, rounded down."""
    return math.floor(t / 60)


@dataclass
class Mode:
    """One way a component, a call between two components, or a pod is failing in a given minute.

    `err` is the share of requests that fail this way, `lat` the milliseconds it adds. `msg` is what
    the logs say when a request fails this way, and `tag` names the fault it comes from: a line or a
    trace that shows a tagged failure is evidence for that fault.
    """

    err: float = 0.0
    lat: float = 0.0
    msg: str = ""
    tag: str | None = None
    status: int = 500
    level: str = "error"


@dataclass
class Minute:
    """Everything the faults decided about one minute, and what follows from it."""

    m: int
    own: dict[str, list[Mode]] = field(default_factory=dict)
    edge: dict[tuple[str, str], list[Mode]] = field(default_factory=dict)
    pod: dict[str, list[Mode]] = field(default_factory=dict)
    touch: dict[tuple[str, str], float] = field(default_factory=dict)
    extra: dict[str, dict[str, float]] = field(default_factory=dict)  # metric overrides, absolute values
    load: dict[str, float] = field(default_factory=dict)  # request multipliers (retry storms)
    # Filled by the world after the faults:
    requests: dict[str, float] = field(default_factory=dict)
    fail: dict[str, float] = field(default_factory=dict)
    lat: dict[str, float] = field(default_factory=dict)
    metrics: dict[str, dict[str, float]] = field(default_factory=dict)

    def add(self, component: str, mode: Mode) -> None:
        self.own.setdefault(component, []).append(mode)

    def add_edge(self, caller: str, target: str, mode: Mode) -> None:
        self.edge.setdefault((caller, target), []).append(mode)

    def add_pod(self, pod: str, mode: Mode) -> None:
        self.pod.setdefault(pod, []).append(mode)

    def set(self, component: str, metric: str, value: float) -> None:
        self.extra.setdefault(component, {})[metric] = value


def combined(modes: list[Mode]) -> float:
    ok = 1.0
    for mode in modes:
        ok *= 1 - max(0.0, min(1.0, mode.err))
    return 1 - ok


@dataclass
class Event:
    """A log line that isn't a request: a reload, a restart, a warning from a database."""

    t: float  # seconds
    source: str
    text: str  # formatted already
    tags: frozenset[str] = frozenset()
    inject: str | None = None  # the call this line tries to talk an agent into, like "restart postgres-primary"


@dataclass
class Act:
    """An action an on-call took, and when it takes effect."""

    n: int  # the console call number
    t: float  # when it was called
    tool: str
    args: dict
    effect: float  # when it's in place
    change_id: str | None = None
