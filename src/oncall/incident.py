"""An incident as files: the page, the change list with its diffs, the logs, and the truth.

    incidents/<name>/page.txt          the page that woke the on-call up
    incidents/<name>/changes.json      today's changes to production, newest first
    incidents/<name>/diffs/<id>.diff   what each change changed
    incidents/<name>/logs/*.log        gateway (nginx access log), catalog-api and playback-api (JSON lines)
    incidents/<name>/truth.json        what really happened: the checks read it, you shouldn't

Every log line gets an id from its file and line number, like `gateway:12`: that's what the
agent cites as evidence.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache

from . import INCIDENTS

NGINX_TIME = re.compile(r"\[(\d{2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2}) [+-]\d{4}\]")


@dataclass(frozen=True)
class LogLine:
    id: str  # "<source>:<line number>", e.g. "gateway:12"
    source: str  # the log file: gateway, catalog-api, playback-api
    number: int  # line number in that file, from 1
    ts: datetime
    text: str


@dataclass(frozen=True)
class Incident:
    name: str
    page: str
    changes: list[dict]
    diffs: dict[str, str]
    lines: list[LogLine]  # every log line of every source, in time order
    truth: dict

    def line(self, line_id: str) -> LogLine | None:
        return self._by_id().get(line_id)

    def change(self, change_id: str) -> dict | None:
        return next((c for c in self.changes if c["id"] == change_id), None)

    @property
    def support(self) -> set[str]:
        """The log lines that show the cause (from the truth)."""
        return {i for ids in self.truth.get("support", {}).values() for i in ids}

    def _by_id(self) -> dict[str, LogLine]:
        return _index(self.name)


def _timestamp(text: str) -> datetime:
    if text.startswith("{"):
        return datetime.fromisoformat(json.loads(text)["ts"].replace("Z", "+00:00"))
    match = NGINX_TIME.search(text)
    if not match:
        raise ValueError(f"no timestamp in log line: {text[:80]!r}")
    return datetime.strptime(match.group(1), "%d/%b/%Y:%H:%M:%S").replace(tzinfo=UTC)


@cache
def load_incident(name: str = "zero") -> Incident:
    folder = INCIDENTS / name
    lines: list[LogLine] = []
    for path in sorted((folder / "logs").glob("*.log")):
        for n, text in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if text.strip():
                lines.append(LogLine(f"{path.stem}:{n}", path.stem, n, _timestamp(text), text))
    lines.sort(key=lambda x: (x.ts, x.source, x.number))
    diffs = {p.stem: p.read_text(encoding="utf-8") for p in sorted((folder / "diffs").glob("*.diff"))}
    truth_path = folder / "truth.json"
    return Incident(
        name=name,
        page=(folder / "page.txt").read_text(encoding="utf-8"),
        changes=json.loads((folder / "changes.json").read_text(encoding="utf-8")),
        diffs=diffs,
        lines=lines,
        truth=json.loads(truth_path.read_text(encoding="utf-8")) if truth_path.exists() else {},
    )


@cache
def _index(name: str) -> dict[str, LogLine]:
    return {line.id: line for line in load_incident(name).lines}
