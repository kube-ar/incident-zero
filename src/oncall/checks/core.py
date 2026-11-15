"""A small check runner: runs your functions on the incident and says what's right and what isn't."""

from __future__ import annotations

import os
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field

from ..milestones import Milestone, lesson_url

MAX_DETAILS = 4


class NotStarted(Exception):
    """Raised when your function still raises NotImplementedError."""


@dataclass
class Result:
    name: str
    lesson: tuple[str, str, str] | None  # (course, slug, title)
    passed: bool = True
    started: bool = True
    score: str = ""
    details: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def fail(self, line: str) -> None:
        self.passed = False
        self.details.append(line)


def attempt(result: Result, fn: Callable, *args, none_ok: bool = False, **kwargs):
    """Call your function. NotImplementedError marks the check as not started.

    A None answer counts as a forgotten return, unless `none_ok` (None is a real answer there).
    """
    try:
        value = fn(*args, **kwargs)
    except NotImplementedError as e:
        result.started = False
        result.passed = False
        raise NotStarted from e
    except Exception:
        result.passed = False
        tb = traceback.format_exc().strip().splitlines()
        result.details.append(f"{getattr(fn, '__name__', 'your code')} raised: {tb[-1]}")
        result.details.extend("    " + line for line in tb[-5:-1])
        return None
    if value is None and not none_ok:
        result.fail(f"{getattr(fn, '__name__', 'your function')} returned None: did it forget a return?")
    return value


def tally(result: Result, right: int, total: int, unit: str) -> None:
    """Score line, and a check passes only when every answer is right."""
    result.score = f"{right}/{total} {unit}"
    if right < total:
        result.passed = False


def _color(code: str, text: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"\033[{code}m{text}\033[0m"


def report(milestone: Milestone, results: list[Result], lang: str = "en") -> bool:
    print()
    print(_color("1", f"Milestone {milestone.id} · {milestone.title}"))
    print(_color("2", f"Instructions: {milestone.url(lang)}"))
    print()
    width = max(len(r.name) for r in results) + 2
    for r in results:
        if not r.started:
            mark, state = _color("2", "·"), _color("2", "not started")
        elif r.passed:
            mark, state = _color("32", "✓"), r.score
        else:
            mark, state = _color("31", "✗"), r.score
        print(f"  {mark} {r.name.ljust(width)}{state}")
        if not r.started:
            for line in r.notes:
                print(_color("2", f"      {line}"))
        if r.started:
            shown = r.details[:MAX_DETAILS]
            for line in shown:
                print(_color("2", f"      {line}"))
            if len(r.details) > len(shown):
                print(_color("2", f"      … and {len(r.details) - len(shown)} more"))
            for line in r.notes:
                print(f"      {line}")
            if not r.passed and r.lesson:
                course, slug, title = r.lesson
                print(_color("36", f"      Read: {title} → {lesson_url(course, slug, lang)}"))
    passed = sum(r.passed for r in results)
    print()
    summary = f"{passed}/{len(results)} checks passed"
    print(_color("1;32" if passed == len(results) else "1", summary))
    if passed == len(results):
        print("Milestone complete. Mark it done on the site and move on to the next one.")
    print()
    return passed == len(results)
