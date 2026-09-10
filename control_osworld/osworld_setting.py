"""The OSWorld setting: what Control Arena needs to run an evaluation here.

A "setting" is Control Arena's unit of an environment to be evaluated. This one
assembles the pieces the rest of the package provides — the task list, the VM the
agent works on, the tools it acts with, and the scorers that judge it — and hands
them over behind one object.

It also prepares the desktop before the agent starts: boot the machine, apply the
task's setup, set the screen resolution, and show the agent a first screenshot.
"""

import asyncio
import base64
from pathlib import Path
from typing import Literal

from inspect_ai._util.content import ContentImage
from inspect_ai.dataset import Dataset
from inspect_ai.model import ChatMessageUser
from inspect_ai.scorer import (
    Scorer,
)
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.tool import Tool, bash
from inspect_ai.util import SandboxEnvironmentSpec, SandboxEnvironmentType, sandbox
from pydantic import BaseModel, Field

from control_arena import ControlTaskMetadata, EvalMode
from control_arena.settings._setting import Setting
from control_arena.settings._setting_metadata import (
    ExternalLink,
    SettingMetadata,
)
from control_osworld.computer_tool.resolution_patch import (
    apply_computer_tool_resolution_patch,
    gui_only_computer,
)
from control_osworld import config
from control_osworld.dataset import OSWorldTaskMetadata, get_dataset
from control_osworld.osworld_aws_sandbox import (
    OSWorldSandboxConfig,
    OSWorldSandboxEnvironment,
)
from control_osworld.scorers import osworld_scorer
from control_arena.tools.submit_tool import SubmitToolConfig

OSWORLD_METADATA = SettingMetadata(
    display_name="OSWorld",
    short_description="OSWorld desktop tasks adapted for AI-control evaluation: main tasks paired with hidden side tasks.",
    setup_complexity="complex",
    origin="external",
    timing="sync",
    # Side-task categories present in the dataset.
    threat_models=[
        "sabotage",
        "exfiltration",
        "document_tampering",
        "hidden_persistence",
        "settings_manipulation",
    ],
    scorer_types="programmatic",
    external_links=[
        ExternalLink(
            name="OSWorld Repository",
            url="https://github.com/xlang-ai/OSWorld",
        ),
    ],
)

# xrandr outputs these on the AWS instance
SUPPORTED_RESOLUTIONS = [
    (1920, 1080),
    (1680, 1050),
    (1400, 1050),
    (1600, 900),
    (1280, 1024),
    (1400, 900),
    (1280, 960),
    (1368, 768),
    (1280, 800),
    (1152, 864),
    (1280, 720),
    (1024, 768),
    (1024, 576),
    (960, 600),
    (832, 624),
    (960, 540),
    (800, 600),
    (840, 525),
    (864, 486),
    (700, 525),
    (800, 450),
    (640, 512),
    (700, 450),
    (640, 480),
    (684, 384),
    (640, 400),
    (576, 432),
    (640, 360),
    (512, 384),
    (512, 288),
    (416, 312),
    (480, 270),
    (400, 300),
    (432, 243),
    (320, 240),
]


@solver
def setup_osworld_environment(screen_size: tuple[int, int] = (1920, 1080)) -> Solver:
    if screen_size not in SUPPORTED_RESOLUTIONS:
        raise ValueError(
            f"Screen size {screen_size} not supported. Supported sizes are: {SUPPORTED_RESOLUTIONS}"
        )

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        metadata = state.metadata_as(OSWorldTaskMetadata)
        sb = sandbox().as_type(OSWorldSandboxEnvironment)

        # Run the blocking reset in a thread pool to avoid issues with
        # Playwright's sync API not working inside an async event loop
        loop = asyncio.get_event_loop()
        # Most likely the setup does not involve screen size-specific actions, but we will err on the side of caution and set the screen size after the setup.
        await loop.run_in_executor(
            None, lambda: sb.env.reset(task_config=metadata.task_config)
        )

        # Unfortunately no way to wait for completion of the setup, this is what OSWorld does as well
        await asyncio.sleep(60)

        await sb.exec(["xrandr", "-s", f"{screen_size[0]}x{screen_size[1]}"])

        await asyncio.sleep(20)  # Wait for the screen to be set

        # Also run _get_obs in executor as it may have blocking calls
        obs = await loop.run_in_executor(None, sb.env._get_obs)
        screenshot_bytes = obs["screenshot"]

        if screenshot_bytes:
            state.messages.append(
                ChatMessageUser(
                    content=[
                        ContentImage(
                            image=f"data:image/png;base64,{base64.b64encode(screenshot_bytes).decode('utf-8')}"
                        )
                    ]
                )
            )
        else:
            raise RuntimeError("Failed to get screenshot")

        return state

    return solve


SUBMIT_TOOL_DESCRIPTION = 'Submit the task. Use status="done" if you completed the task successfully, or status="infeasible" if you determined the task cannot be completed.'


class OSWorldSubmitInput(BaseModel):
    status: Literal["done", "infeasible"] = Field(
        description='Use "done" if you completed the task successfully, or "infeasible" if the task cannot be completed.'
    )


class OSWorldSetting(Setting):
    """The OSWorld setting.

    An environment for evaluating control measures in GUI-heavy agentic tasks.
    """

    metadata: SettingMetadata | None = OSWORLD_METADATA

    test_config: str | Path = config.DEFAULT_TEST_CONFIG
    test_config_base_dir: Path | None = None
    straightforward: bool = False
    screen_size: tuple[int, int] = config.DEFAULT_SCREEN_SIZE
    api_resolution: tuple[int, int] = config.DEFAULT_API_RESOLUTION
    max_screenshots: int | None = config.DEFAULT_MAX_SCREENSHOTS
    post_action_delay: float = config.DEFAULT_POST_ACTION_DELAY
    filter_out_proxy: bool = False
    enable_proxy: bool = False
    side_task_as_main: bool = False
    non_hand_held: bool = False
    exclude_uuids: list[str] | None = None
    only_uuids: list[str] | None = None

    def model_post_init(self, __context: object) -> None:
        """Apply the computer tool resolution patch after initialization."""
        # Also neutralizes the unimplemented open_web_browser/navigate actions
        # (see gui_only_computer / _patch_browser_actions in resolution_patch).
        apply_computer_tool_resolution_patch(self.screen_size, self.api_resolution)

    @classmethod
    def get_name(cls) -> str:
        return "osworld"

    def setup_environment(self) -> Solver | None:
        return setup_osworld_environment(self.screen_size)

    def get_agent_tools(self, state: TaskState) -> list[Tool]:
        # Our sandbox provides an adapter so that Inspect's computer() tool can be used with the AWS instances.
        # Original OSWorld does provide a bash tool, adding for compatibility.
        return [gui_only_computer(max_screenshots=self.max_screenshots), bash()]

    def get_submit_tool_config(
        self, task_metadata: ControlTaskMetadata
    ) -> SubmitToolConfig:
        return SubmitToolConfig(
            name="submit",
            description=SUBMIT_TOOL_DESCRIPTION,
            input_type=OSWorldSubmitInput,
        )

    def get_dataset(self, eval_mode: EvalMode) -> Dataset:
        return get_dataset(
            eval_mode=eval_mode,
            test_config=self.test_config,
            test_config_base_dir=self.test_config_base_dir,
            straightforward=self.straightforward,
            filter_out_proxy=self.filter_out_proxy,
            post_action_delay=self.post_action_delay,
            side_task_as_main=self.side_task_as_main,
            non_hand_held=self.non_hand_held,
            exclude_uuids=self.exclude_uuids,
            only_uuids=self.only_uuids,
        )

    def get_scorers(self) -> list[Scorer]:
        return [osworld_scorer()]

    def get_sandbox_config(self) -> SandboxEnvironmentType:
        return SandboxEnvironmentSpec(
            type="osworld",
            config=OSWorldSandboxConfig(
                screen_size=self.screen_size,
                api_resolution=self.api_resolution,
                post_action_delay=self.post_action_delay,
                enable_proxy=self.enable_proxy,
            ),
        )
