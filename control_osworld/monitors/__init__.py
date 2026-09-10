"""Trusted monitors for the OSWorld setting, as Control Arena ControlAgents.

Two kinds: one scores a finished run in a single call, the other scores it step by
step. Both run either live during an evaluation or afterwards over a saved log.

What the monitor is shown of the run is chosen by `variant` -- screenshots, the
agent's visible text, or both. Its private reasoning is never shown.
"""

from control_osworld.monitors._full_trajectory import osworld_full_trajectory_monitor
from control_osworld.monitors._per_step import osworld_per_step_monitor
from control_osworld.monitors.prompts import monitor_prompts

__all__ = [
    "osworld_full_trajectory_monitor",
    "osworld_per_step_monitor",
    "monitor_prompts",
]
