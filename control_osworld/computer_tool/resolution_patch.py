"""Monkey patch to fix inspect_ai's hardcoded computer tool resolution.

Inspect_ai hardcodes the display resolution to 1366x768. This patch overrides
the relevant method to use OSWorld's display resolution of 1920x1080 (or any
other chosen resolution).

We also handle model-specific requirements for resolution:
  - Anthropic: expects a smaller resolution than the native VM display. We mimic
    OSWorld's implementation and use api_resolution to downscale — screenshots are
    resized from screen_size to api_resolution before being sent to the model, and
    coordinates returned by the model are scaled back up. The actual resizing and
    coordinate scaling is done in computer_tool/vm_script.py on the VM.
  - OpenAI: resolution is determined by the screenshot dimensions sent back, so
    no patching is needed. The resizing in computer_tool/vm_script.py is sufficient.

Usage:
    from control_osworld.computer_tool.resolution_patch import (
        apply_computer_tool_resolution_patch,
    )
    apply_computer_tool_resolution_patch(
        screen_size=(1920, 1080),
        api_resolution=(1280, 720),
    )
"""

import logging
from typing import TYPE_CHECKING

from packaging.version import Version

if TYPE_CHECKING:
    from inspect_ai.tool import ToolInfo

logger = logging.getLogger(__name__)

# inspect_ai version this patch was written against. The patch overrides
# AnthropicAPI.computer_use_tool_param, whose signature/behaviour is internal to
# inspect_ai and can change between releases — so _check_inspect_ai_version() errors
# on older versions and warns on newer ones. Bump this (and re-verify the override
# in _apply_anthropic_patch) whenever inspect_ai is upgraded.
EXPECTED_INSPECT_AI_VERSION = "0.3.233"

# Module-level state set by apply_computer_tool_resolution_patch
_screen_size: tuple[int, int] = (1920, 1080)
_api_resolution: tuple[int, int] = (1920, 1080)


def get_screen_size() -> tuple[int, int]:
    """Get the configured VM screen size."""
    return _screen_size


def get_api_resolution() -> tuple[int, int]:
    """Get the configured API resolution."""
    return _api_resolution


def _check_inspect_ai_version() -> None:
    """Check that inspect_ai version is compatible with this patch."""
    try:
        from importlib.metadata import version
        inspect_ai_version = version("inspect_ai")
    except Exception as e:
        logger.warning(f"Could not determine inspect_ai version: {e}")
        return

    current = Version(inspect_ai_version)
    expected = Version(EXPECTED_INSPECT_AI_VERSION)

    if current < expected:
        raise RuntimeError(
            f"inspect_ai version {inspect_ai_version} is older than the required "
            f"version {EXPECTED_INSPECT_AI_VERSION}. Please update inspect_ai:\n"
            f"  pip install --upgrade inspect_ai>={EXPECTED_INSPECT_AI_VERSION}"
        )
    elif current > expected:
        logger.warning(
            f"inspect_ai version {inspect_ai_version} is newer than the version "
            f"this patch was written for ({EXPECTED_INSPECT_AI_VERSION}). "
            f"Please verify that the computer_use_tool_param monkey patch in "
            f"computer_tool/resolution_patch.py is still valid for this version."
        )


def _apply_anthropic_patch() -> None:
    """Apply the resolution patch for Anthropic's computer tool."""
    try:
        from anthropic.types.beta import (
            BetaToolComputerUse20250124Param,
            BetaToolComputerUse20251124Param,
        )
        from inspect_ai.model._providers.anthropic import AnthropicAPI
        from inspect_ai.tool._tools._computer._computer import is_computer_tool_info
    except ImportError as e:
        logger.warning(
            f"Could not import Anthropic modules for resolution patch: {e}"
        )
        return

    def patched_computer_use_tool_param(
        self: AnthropicAPI, tool: "ToolInfo"
    ) -> BetaToolComputerUse20250124Param | BetaToolComputerUse20251124Param | None:
        """Patched version that uses the configured api_resolution."""
        if not is_computer_tool_info(tool):
            return None

        if self.is_claude_3_5():
            return None

        width, height = _api_resolution

        # computer_20251124 is supported by Claude 4.6+ (incl. Opus 4.7/4.8) and
        # Claude Opus 4.5. Opus 4.8 REJECTS the old computer_20250124 (400), so
        # route 4.7-or-later through the new tool param too.
        if (
            self.is_claude_4_6()
            or self.is_claude_4_7_or_later()
            or self.is_claude_latest()
            or (self.is_claude_4_5() and self.is_claude_4_opus())
        ):
            return BetaToolComputerUse20251124Param(
                type="computer_20251124",
                name="computer",
                display_width_px=width,
                display_height_px=height,
                display_number=1,
                enable_zoom=True,
            )
        else:
            return BetaToolComputerUse20250124Param(
                type="computer_20250124",
                name="computer",
                display_width_px=width,
                display_height_px=height,
                display_number=1,
            )

    # Apply the patch
    AnthropicAPI.computer_use_tool_param = patched_computer_use_tool_param  # type: ignore[method-assign]
    logger.info(f"Applied Anthropic computer tool resolution patch: {_api_resolution[0]}x{_api_resolution[1]}")


def apply_computer_tool_resolution_patch(
    screen_size: tuple[int, int],
    api_resolution: tuple[int, int],
) -> None:
    """Apply monkey patches to fix the computer tool resolution in inspect_ai.

    This patches the Anthropic provider to use api_resolution instead of the
    hardcoded 1366x768.

    Args:
        screen_size: The actual VM display resolution (e.g. 1920x1080).
        api_resolution: The resolution reported to the model API (e.g. 1280x720).
            Screenshots are resized to this resolution and model coordinates
            are scaled back to screen_size in computer_tool/vm_script.py.

    Raises:
        RuntimeError: If inspect_ai version is older than expected.
    """
    _check_inspect_ai_version()

    global _screen_size, _api_resolution
    _screen_size = screen_size
    _api_resolution = api_resolution

    _apply_anthropic_patch()
    _patch_browser_actions()

    logger.info(
        f"Computer tool resolution: screen_size={screen_size[0]}x{screen_size[1]}, "
        f"api_resolution={api_resolution[0]}x{api_resolution[1]}"
    )


# --- open_web_browser / navigate: not implemented by vm_script.py and a non-GUI
# confound. gui_only_computer() drops them from the advertised schema; the patch
# below is a safety net so a stray call returns a GUI hint instead of crashing. ---

_BLOCKED_ACTIONS = ("open_web_browser", "navigate")


def gui_only_computer(max_screenshots: int | None = 1, timeout: int | None = 180):
    """inspect's computer() tool with the non-GUI browser shortcuts removed from
    the advertised schema (enum + description), so the agent opens the browser via
    the GUI (per the setting prompt) like everything else."""
    import re

    from inspect_ai.tool import ToolDef, computer

    td = ToolDef(computer(max_screenshots=max_screenshots, timeout=timeout))
    action = td.parameters.properties["action"]

    for sub in action.anyOf or []:
        if sub.enum:
            sub.enum = [a for a in sub.enum if a not in _BLOCKED_ACTIONS]
    if action.enum:
        action.enum = [a for a in action.enum if a not in _BLOCKED_ACTIONS]

    bullet = re.compile(r"^-\s+`([a-z_]+)`")
    kept, skip = [], False
    for line in (action.description or "").split("\n"):
        m = bullet.match(line.strip())
        if m:
            skip = m.group(1) in _BLOCKED_ACTIONS
        if not skip:
            kept.append(line)
    action.description = "\n".join(kept)

    return td.as_tool()


_browser_actions_patched = False


def _patch_browser_actions() -> None:
    """Make common.open_web_browser/navigate return a GUI hint instead of hitting
    the VM (which rejects them). Idempotent; a safety net for models that call the
    actions from priors despite the trimmed schema."""
    global _browser_actions_patched
    if _browser_actions_patched:
        return
    from inspect_ai.tool._tools._computer import _common as common

    async def _open_web_browser(timeout: int | None = None) -> str:  # noqa: ARG001
        return ("The `open_web_browser` action is not available. Open the browser "
                "by clicking the Chrome icon on the desktop.")

    async def _navigate(text: str, timeout: int | None = None) -> str:  # noqa: ARG001
        return ("The `navigate` action is not available. Use the browser GUI: click "
                "the address bar, type the URL, and press Enter.")

    common.open_web_browser = _open_web_browser
    common.navigate = _navigate
    _browser_actions_patched = True
    logger.info("Patched computer tool: open_web_browser/navigate return GUI hints.")
