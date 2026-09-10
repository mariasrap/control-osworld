#!/usr/bin/env python3
"""Compatibility script for Inspect AI's computer() tool.

This script should be deployed to /opt/inspect/tool/computer_tool.py on the OSWorld VM.
It translates Inspect's CLI commands to pyautogui calls.

Usage:
    python3 /opt/inspect/tool/computer_tool.py left_click --coordinate 100 200
    python3 /opt/inspect/tool/computer_tool.py type --text "Hello World"
    python3 /opt/inspect/tool/computer_tool.py screenshot
"""

import argparse
import base64
import io
import json
import sys
import time

import pyautogui
from PIL import Image

# Disable pyautogui's fail-safe (moving mouse to corner stops program)
pyautogui.FAILSAFE = False

# Resolution configuration — these values are replaced at deploy time by
# deploy_computer_tool() in osworld_aws_sandbox.py.
# SCREEN_SIZE: the actual VM display resolution.
# API_RESOLUTION: the resolution reported to the model API. When different from
# SCREEN_SIZE, screenshots are resized and coordinates are scaled accordingly.
SCREEN_SIZE = (1920, 1080)
API_RESOLUTION = (1920, 1080)

# Key mappings for pyautogui
KEY_MAPPING = {
    "Return": "enter",
    "Escape": "esc",
    "BackSpace": "backspace",
    "Tab": "tab",
    "Left": "left",
    "Right": "right",
    "Up": "up",
    "Down": "down",
    "space": "space",
}

POST_ACTION_DELAY = 0.5  # seconds


def scale_coordinates(x: int, y: int) -> tuple[int, int]:
    """Scale coordinates from API_RESOLUTION space to SCREEN_SIZE space."""
    if API_RESOLUTION == SCREEN_SIZE:
        return x, y
    scale_x = SCREEN_SIZE[0] / API_RESOLUTION[0]
    scale_y = SCREEN_SIZE[1] / API_RESOLUTION[1]
    return round(x * scale_x), int(y * scale_y)


def take_screenshot() -> str | None:
    """Take a screenshot, resize to API_RESOLUTION, and return as base64."""
    try:
        time.sleep(POST_ACTION_DELAY)
        screenshot = pyautogui.screenshot()
        if API_RESOLUTION != SCREEN_SIZE:
            screenshot = screenshot.resize(API_RESOLUTION, Image.LANCZOS)
        buffer = io.BytesIO()
        screenshot.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("utf-8")
    except Exception as e:
        print(f"Screenshot error: {e}", file=sys.stderr)
        return None


def result_json(
    output: str | None = None,
    error: str | None = None,
    include_screenshot: bool = True,
) -> str:
    """Build the result JSON that Inspect expects."""
    return json.dumps(
        {
            "output": output,
            "error": error,
            "base64_image": take_screenshot() if include_screenshot else None,
        }
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect AI computer tool compatibility layer"
    )

    # Action is the first positional argument
    parser.add_argument(
        "action",
        choices=[
            "key",
            "hold_key",
            "type",
            "cursor_position",
            "mouse_move",
            "left_mouse_down",
            "left_mouse_up",
            "left_click",
            "left_click_drag",
            "right_click",
            "middle_click",
            "back_click",
            "forward_click",
            "double_click",
            "triple_click",
            "scroll",
            "wait",
            "screenshot",
            "zoom",
        ],
    )

    # Optional arguments
    parser.add_argument("--coordinate", nargs=2, type=int, metavar=("X", "Y"))
    parser.add_argument("--start_coordinate", nargs=2, type=int, metavar=("X", "Y"))
    parser.add_argument("--text", type=str)
    parser.add_argument("--duration", type=int)
    parser.add_argument("--scroll_amount", type=int)
    parser.add_argument("--scroll_direction", choices=["up", "down", "left", "right"])
    parser.add_argument(
        "--region", nargs=4, type=int, metavar=("X0", "Y0", "X1", "Y1")
    )

    return parser.parse_args()


def handle_screenshot() -> None:
    print(result_json(output="Screenshot taken"))


def handle_cursor_position() -> None:
    x, y = pyautogui.position()
    print(result_json(output=f"Cursor position: ({x}, {y})"))


def handle_mouse_move(args: argparse.Namespace) -> None:
    if not args.coordinate:
        print(result_json(error="coordinate is required for mouse_move"))
        sys.exit(1)
    x, y = args.coordinate
    pyautogui.moveTo(x, y)
    print(result_json(output=f"Moved to ({x}, {y})"))


def handle_left_click(args: argparse.Namespace) -> None:
    if args.coordinate:
        x, y = args.coordinate
        pyautogui.click(x, y, button="left")
        print(result_json(output=f"Left clicked at ({x}, {y})"))
    else:
        pyautogui.click(button="left")
        print(result_json(output="Left clicked at current position"))


def handle_right_click(args: argparse.Namespace) -> None:
    if args.coordinate:
        x, y = args.coordinate
        pyautogui.click(x, y, button="right")
        print(result_json(output=f"Right clicked at ({x}, {y})"))
    else:
        pyautogui.click(button="right")
        print(result_json(output="Right clicked at current position"))


def handle_middle_click(args: argparse.Namespace) -> None:
    if args.coordinate:
        x, y = args.coordinate
        pyautogui.click(x, y, button="middle")
        print(result_json(output=f"Middle clicked at ({x}, {y})"))
    else:
        pyautogui.click(button="middle")
        print(result_json(output="Middle clicked at current position"))


def handle_double_click(args: argparse.Namespace) -> None:
    if args.coordinate:
        x, y = args.coordinate
        pyautogui.doubleClick(x, y)
        print(result_json(output=f"Double clicked at ({x}, {y})"))
    else:
        pyautogui.doubleClick()
        print(result_json(output="Double clicked at current position"))


def handle_triple_click(args: argparse.Namespace) -> None:
    if args.coordinate:
        x, y = args.coordinate
        pyautogui.tripleClick(x, y)
        print(result_json(output=f"Triple clicked at ({x}, {y})"))
    else:
        pyautogui.tripleClick()
        print(result_json(output="Triple clicked at current position"))


def handle_left_click_drag(args: argparse.Namespace) -> None:
    if not args.start_coordinate or not args.coordinate:
        print(
            result_json(
                error="start_coordinate and coordinate are required for left_click_drag"
            )
        )
        sys.exit(1)
    sx, sy = args.start_coordinate
    ex, ey = args.coordinate
    pyautogui.moveTo(sx, sy)
    pyautogui.drag(ex - sx, ey - sy, duration=0.5)
    print(result_json(output=f"Dragged from ({sx}, {sy}) to ({ex}, {ey})"))


def handle_left_mouse_down() -> None:
    pyautogui.mouseDown(button="left")
    print(result_json(output="Left mouse button pressed"))


def handle_left_mouse_up() -> None:
    pyautogui.mouseUp(button="left")
    print(result_json(output="Left mouse button released"))


def handle_back_click(args: argparse.Namespace) -> None:
    # Back button (button 8 on X11, not directly supported by pyautogui)
    if args.coordinate:
        x, y = args.coordinate
        pyautogui.moveTo(x, y)
    import subprocess

    subprocess.run(["/usr/bin/xdotool", "click", "8"], check=True)
    print(result_json(output="Back button clicked"))


def handle_forward_click(args: argparse.Namespace) -> None:
    # Forward button (button 9 on X11)
    if args.coordinate:
        x, y = args.coordinate
        pyautogui.moveTo(x, y)
    import subprocess

    subprocess.run(["/usr/bin/xdotool", "click", "9"], check=True)
    print(result_json(output="Forward button clicked"))


def handle_type(args: argparse.Namespace) -> None:
    if not args.text:
        print(result_json(error="text is required for type action"))
        sys.exit(1)
    pyautogui.typewrite(args.text, interval=0.02)
    truncated = args.text[:50] + ("..." if len(args.text) > 50 else "")
    print(result_json(output=f"Typed: {truncated}"))


def handle_key(args: argparse.Namespace) -> None:
    if not args.text:
        print(result_json(error="text is required for key action"))
        sys.exit(1)
    # Handle key combinations like "ctrl+s"
    keys = args.text.split("+")
    mapped_keys = [KEY_MAPPING.get(k, k.lower()) for k in keys]
    pyautogui.hotkey(*mapped_keys)
    print(result_json(output=f"Pressed: {args.text}"))


def handle_hold_key(args: argparse.Namespace) -> None:
    if not args.text or args.duration is None:
        print(result_json(error="text and duration are required for hold_key action"))
        sys.exit(1)
    keys = args.text.split("+")
    mapped_keys = [KEY_MAPPING.get(k, k.lower()) for k in keys]
    # Hold keys down
    for key in mapped_keys:
        pyautogui.keyDown(key)
    time.sleep(args.duration)
    # Release keys
    for key in reversed(mapped_keys):
        pyautogui.keyUp(key)
    print(result_json(output=f"Held {args.text} for {args.duration}s"))


def handle_scroll(args: argparse.Namespace) -> None:
    if args.scroll_amount is None or not args.scroll_direction:
        print(
            result_json(
                error="scroll_amount and scroll_direction are required for scroll action"
            )
        )
        sys.exit(1)

    # Move to coordinate first if provided
    if args.coordinate:
        x, y = args.coordinate
        pyautogui.moveTo(x, y)

    amount = args.scroll_amount
    direction = args.scroll_direction

    if direction == "up":
        pyautogui.scroll(amount)
    elif direction == "down":
        pyautogui.scroll(-amount)
    elif direction == "left":
        pyautogui.hscroll(-amount)
    elif direction == "right":
        pyautogui.hscroll(amount)

    print(result_json(output=f"Scrolled {direction} by {amount}"))


def handle_wait(args: argparse.Namespace) -> None:
    if args.duration is None:
        print(result_json(error="duration is required for wait action"))
        sys.exit(1)
    time.sleep(args.duration)
    print(result_json(output=f"Waited {args.duration}s"))


def handle_zoom(args: argparse.Namespace) -> None:
    """Take a zoomed screenshot of a specific region at native resolution."""
    if not args.region:
        print(result_json(error="region is required for zoom action"))
        sys.exit(1)

    x0, y0, x1, y1 = args.region

    # Validate region coordinates
    if x0 >= x1 or y0 >= y1:
        print(result_json(error=f"Invalid region: ({x0}, {y0}, {x1}, {y1})"))
        sys.exit(1)

    try:
        # Take a full screenshot
        screenshot = pyautogui.screenshot()

        # Crop to the specified region (PIL crop uses (left, upper, right, lower))
        cropped = screenshot.crop((x0, y0, x1, y1))

        # Encode to base64
        buffer = io.BytesIO()
        cropped.save(buffer, format="PNG")
        base64_image = base64.b64encode(buffer.getvalue()).decode("utf-8")

        print(
            json.dumps(
                {
                    "output": f"Zoomed screenshot of region ({x0}, {y0}, {x1}, {y1})",
                    "error": None,
                    "base64_image": base64_image,
                }
            )
        )
    except Exception as e:
        print(result_json(error=f"Zoom error: {e}"))
        sys.exit(1)


def main() -> None:
    try:
        args = parse_args()
        action = args.action

        # Scale all coordinates from API space to screen space.
        if args.coordinate:
            args.coordinate = list(scale_coordinates(*args.coordinate))
        if args.start_coordinate:
            args.start_coordinate = list(scale_coordinates(*args.start_coordinate))
        if args.region:
            x0, y0 = scale_coordinates(args.region[0], args.region[1])
            x1, y1 = scale_coordinates(args.region[2], args.region[3])
            args.region = [x0, y0, x1, y1]

        handlers = {
            "screenshot": lambda: handle_screenshot(),
            "cursor_position": lambda: handle_cursor_position(),
            "mouse_move": lambda: handle_mouse_move(args),
            "left_click": lambda: handle_left_click(args),
            "right_click": lambda: handle_right_click(args),
            "middle_click": lambda: handle_middle_click(args),
            "double_click": lambda: handle_double_click(args),
            "triple_click": lambda: handle_triple_click(args),
            "left_click_drag": lambda: handle_left_click_drag(args),
            "left_mouse_down": lambda: handle_left_mouse_down(),
            "left_mouse_up": lambda: handle_left_mouse_up(),
            "back_click": lambda: handle_back_click(args),
            "forward_click": lambda: handle_forward_click(args),
            "type": lambda: handle_type(args),
            "key": lambda: handle_key(args),
            "hold_key": lambda: handle_hold_key(args),
            "scroll": lambda: handle_scroll(args),
            "wait": lambda: handle_wait(args),
            "zoom": lambda: handle_zoom(args),
        }

        handler = handlers.get(action)
        if handler:
            handler()
        else:
            print(result_json(error=f"Unknown action: {action}"))
            sys.exit(1)

    except Exception as e:
        print(result_json(error=str(e)))
        sys.exit(1)


if __name__ == "__main__":
    main()
