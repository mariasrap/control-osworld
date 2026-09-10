"""Register the OSWorld setting with Control Arena's plugin system.

Referenced by the ``control_arena`` entry point in pyproject.toml. Experiments
import ``OSWorldSetting`` directly; this exists for plugin-based discovery.
"""

from control_osworld.osworld_setting import OSWorldSetting

settings = [OSWorldSetting]
