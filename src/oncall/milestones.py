"""The project's milestones: where your code goes, where its checks live, where to read about it."""

from __future__ import annotations

from dataclasses import dataclass

SITE = "https://kube.ar"
PROJECT = "incident-zero"


@dataclass(frozen=True)
class Milestone:
    id: str
    course: str  # the course on the site this milestone closes a chapter of
    slug: str
    title: str
    module: str  # your code
    reference: str  # the reference solution, relative to the repo root
    checks: str  # the module with `run(module)`
    extra: tuple[str, ...] = ()  # files next to your module that the reference also replaces

    @property
    def file(self) -> str:
        return "src/" + self.module.replace(".", "/") + ".py"

    def url(self, lang: str = "en") -> str:
        prefix = "" if lang == "en" else f"/{lang}"
        return f"{SITE}{prefix}/{self.course}/project/{PROJECT}/{self.slug}"


def _m(n: int, slug: str, title: str) -> Milestone:
    """A milestone of part 1, the on-call agent (the AI Agents course)."""
    name = f"m{n}_{slug.replace('-', '_')}"
    return Milestone(str(n), "agents", slug, title, f"oncall.agent.{name}", f"reference/agent/{name}.py",
                     f"oncall.checks.m{n}")  # fmt: skip


MILESTONES = {
    m.id: m
    for m in [
        _m(0, "was-it-the-deploy", "Was it the deploy?"),
        _m(1, "the-first-page", "The first page"),
    ]
}


def lesson_url(course: str, slug: str, lang: str = "en") -> str:
    prefix = "" if lang == "en" else f"/{lang}"
    return f"{SITE}{prefix}/{course}/{slug}"
