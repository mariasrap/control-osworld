"""OSWorld setting for control_arena.

This setting wraps OSWorld as an Inspect AI sandbox environment for GUI-based
agent evaluation with computer use capabilities.
"""

from control_osworld.osworld_aws_sandbox import (
    OSWorldSandboxConfig,
    OSWorldSandboxEnvironment,
)
from control_osworld.osworld_setting import OSWorldSetting

__all__ = [
    "OSWorldSandboxConfig",
    "OSWorldSandboxEnvironment",
    "OSWorldSetting",
]
