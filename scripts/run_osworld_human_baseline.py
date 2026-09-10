#!/usr/bin/env python
"""
Run OSWorld human baseline for testing side task feasibility.

This script allows you to manually test OSWorld side tasks by connecting
to the VM via VNC and completing tasks yourself.

Usage:
    # Basic usage - test a single task in honest mode
    python scripts/run_osworld_human_baseline.py --test_config test_single

    # Test in attack mode (shows side task instructions)
    python scripts/run_osworld_human_baseline.py --test_config test_single --mode attack

    # Specify a specific sample
    python scripts/run_osworld_human_baseline.py --test_config test_small --sample_id chrome_abc123

    # With custom timeout (in seconds)
    python scripts/run_osworld_human_baseline.py --test_config test_single --timeout 3600

Notes:
    - The VNC URL will be displayed when the environment is ready
    - Open the URL in your browser for GUI access
    - Complete the task using the GUI
    - Signal completion by running: touch ~/TASK_DONE (or ~/TASK_INFEASIBLE)
    - The evaluation will be scored automatically
"""

import argparse
import logging

from inspect_ai import eval

from control_arena import EvalMode
from control_arena.eval import ControlEvalConfig, get_control_task_product
from control_osworld.osworld_setting import OSWorldSetting
from control_osworld.policy.human_policy import osworld_human_policy


def main(args: argparse.Namespace) -> None:
    setting = OSWorldSetting(
        test_config=args.test_config,
        straightforward=False,
        screen_size=tuple(args.screen_size),
        filter_out_proxy=args.filter_out_proxy,
        side_task_as_main=args.side_task_as_main,
    )

    # Determine mode
    if args.mode == "honest":
        modes = [EvalMode.HONEST]
        include_side_task = False
    elif args.mode == "attack":
        modes = [EvalMode.ATTACK]
        include_side_task = True
    else:
        raise ValueError(f"Invalid mode: {args.mode}")

    # Create human GUI agent builder
    human_agent_builder = osworld_human_policy(
        include_side_task=include_side_task,
        poll_interval=args.poll_interval,
        timeout=args.timeout,
        write_instructions_to_vm=True,
    )

    # Use a dummy model since human is doing the work
    eval_config = ControlEvalConfig(
        max_steps=1,  # Human does everything in one "step"
        max_sandboxes=1,
        untrusted_model="anthropic/claude-haiku-4-5",  # Not used, but required
        trusted_model="anthropic/claude-haiku-4-5",  # Not used, but required
    )

    eval_tasks = get_control_task_product(
        settings=setting,
        micro_protocols=human_agent_builder,
        modes=modes,
        eval_config=eval_config,
    )

    # Run evaluation
    eval_kwargs = {
        "display": "full",
        "sandbox_cleanup": not args.no_cleanup,
    }

    if args.sample_id:
        eval_kwargs["sample_id"] = args.sample_id

    eval(eval_tasks, **eval_kwargs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run OSWorld human baseline for testing side tasks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--test_config",
        type=str,
        default="test_single",
        help="Test configuration file name or path (default: test_single)",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default="attack",
        choices=["honest", "attack"],
        help="Evaluation mode. Use 'attack' to see side task instructions (default: attack)",
    )
    parser.add_argument(
        "--sample_id",
        type=str,
        default=None,
        help="Specific sample ID to test (e.g., chrome_abc123)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Timeout in seconds (default: no timeout)",
    )
    parser.add_argument(
        "--poll_interval",
        type=float,
        default=2.0,
        help="How often to check for completion markers in seconds (default: 2.0)",
    )
    parser.add_argument(
        "--screen_size",
        type=int,
        nargs=2,
        default=[1920, 1080],
        help="Screen size as width height (default: 1920 1080)",
    )
    parser.add_argument(
        "--filter_out_proxy",
        action="store_true",
        help="Filter out proxy tasks from evaluation",
    )
    parser.add_argument(
        "--side_task_as_main",
        action="store_true",
        help="Run side tasks as standalone tasks (agent sees only the side task).",
    )
    parser.add_argument(
        "--no_cleanup",
        action="store_true",
        help="Don't cleanup sandbox after evaluation (useful for debugging)",
    )
    parser.add_argument(
        "--loglevel",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level (default: INFO)",
    )

    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.loglevel))

    main(args)
