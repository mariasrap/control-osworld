"""Agents that drive the OSWorld desktop: no-op, human, and the attack wrapper."""

from control_osworld.policy.empty_policy import osworld_empty_policy
from control_osworld.policy.human_policy import osworld_human_policy
from control_osworld.policy.prompts import policy_prompts

__all__ = [
    "osworld_empty_policy",
    "osworld_human_policy",
    "policy_prompts",
]
