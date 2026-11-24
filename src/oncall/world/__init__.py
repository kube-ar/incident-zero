"""Reelvane in a box: a seeded simulation of its production, with faults injected and a clock that runs.

    from oncall.world import World, Spec
    world = World(Spec("x1", seed=7, faults=[{"family": "config_change"}]))

The console (oncall.live) is how an agent sees and touches it; the checks read `world.faults`
for the truth. You never need to import this module for a milestone.
"""

from .base import Act, Event, Minute, Mode
from .sim import Change, Line, Spec, Trace, World

__all__ = ["Act", "Change", "Event", "Line", "Minute", "Mode", "Spec", "Trace", "World"]
