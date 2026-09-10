"""Shared constants for the OSWorld setting.

These are the single source of truth for the defaults: the setting's fields, the
dataset builder and scripts/run_osworld.py all read them, so a change lands in
every entry point at once instead of having to be mirrored by hand.
"""

from pathlib import Path

# Where the task data lives.
EVALUATION_EXAMPLES_DIR = Path(__file__).parent / "evaluation_examples"
HEAVY_TASKS_FILE = EVALUATION_EXAMPLES_DIR / "heavy_tasks.json"
DEFAULT_TEST_CONFIG = "test_single"

# What the machine looks like to the agent. api_resolution is the size the
# screenshots are sent at, which some models want smaller than the real screen.
DEFAULT_SCREEN_SIZE = (1920, 1080)
DEFAULT_API_RESOLUTION = (1920, 1080)
DEFAULT_MAX_SCREENSHOTS = 10
DEFAULT_POST_ACTION_DELAY = 3.0

# How much a run is allowed to spend. These bound the eval rather than the
# setting, so only the CLI reads them.
DEFAULT_MAX_STEPS = 75
DEFAULT_TOKEN_LIMIT = 1_000_000

# t3 is the only instance family we launch; the heavy-task guard compares a
# task's required size against the one we are running on.
DEFAULT_INSTANCE_TYPE = "t3.xlarge"
T3_MEM_GIB = {"t3.medium": 4, "t3.large": 8, "t3.xlarge": 16, "t3.2xlarge": 32}
