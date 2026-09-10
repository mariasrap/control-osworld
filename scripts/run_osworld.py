"""Run an OSWorld control evaluation and write an inspect .eval log.

Runs an agent on OSWorld GUI tasks. Ways to run it (see --help for every flag):

- honest (--mode honest): the agent does the main task only.
- attack (--mode attack): the agent also pursues a hidden side task. Give it an
  attack prompt with --attack_prompt_file, and optionally a reminder injected every
  few steps with --attack_reminders --attack_reminder_file. Without a prompt file it
  uses the built-in default attack prompt.
- empty agent (--empty_agent): a no-op agent that submits immediately, honest or
  attack, for checking the pipeline and scoring rather than the model.
- side task as main (--side_task_as_main, honest only): present the side task as the
  agent's only task and score just it.
- side task verifier (--side_task_verifier_agent): attack mode where the agent does
  the side task openly and ignores the main task, to check it can be done and scored.
- per-step side scoring (--per_step_side_eval): in any attack run, also score the
  side task after every step, not just at the end.
"""
import argparse
import json
import logging
import os
from pathlib import Path

from inspect_ai import eval
from inspect_ai.model import GenerateConfig

from control_arena import EvalMode
from control_arena.eval import ControlEvalConfig, get_control_task_product
from control_arena.policy import attack_policy_builder, honest_policy_builder, untrusted_policy_builder
from control_osworld import config
from control_osworld.dataset import EVALUATION_EXAMPLES_DIR
from control_osworld.osworld_setting import OSWorldSetting
from control_osworld.osworld_aws_sandbox import _PROXY_CONFIG_PATH
from control_osworld.policy.attack_reminders import osworld_attack_reminder
from control_osworld.scorers import osworld_per_step_side_eval
from control_osworld.policy import (
    policy_prompts as osworld_policy_prompts,
)
from control_osworld.policy.empty_policy import osworld_empty_policy

# Per-provider overrides. screen_size is not here: --screen_size always supplies
# one, so a provider default would never be reached.
MODEL_DEFAULTS = {
    "default": {
        "generate_config": lambda: GenerateConfig(),
    },
    "anthropic": {
        "api_resolution": (1280, 720),
        "generate_config": lambda: GenerateConfig(reasoning_tokens=2048),
    },
    "openai": {
        "generate_config": lambda: GenerateConfig(reasoning_effort="medium", reasoning_summary="detailed"),
    },
}


def _get_model_defaults(model: str) -> dict:
    defaults = dict(MODEL_DEFAULTS["default"])
    for provider, overrides in MODEL_DEFAULTS.items():
        if provider != "default" and provider in model:
            defaults.update(overrides)
            break
    return defaults


# A few OSWorld tasks (e.g. some libreoffice_calc ones) OOM the default sandbox
# and need a bigger EC2 instance. heavy_tasks.json maps each such uuid to the
# instance type it needs; the guard below keeps them off too-small runs and
# prints the command to run them large. t3 is the only family we launch.


def _config_uuids(test_config: str) -> set[str]:
    path = Path(test_config) if Path(test_config).is_file() else EVALUATION_EXAMPLES_DIR / f"{test_config}.json"
    return {u for us in json.loads(path.read_text()).values() for u in us}


def _heavy_guard(args: argparse.Namespace) -> tuple[set[str] | None, set[str] | None]:
    """Route memory-heavy tasks to a big-enough VM, using heavy_tasks.json.

    A few tasks run out of memory on a small VM; heavy_tasks.json records each one's
    UUID and the smallest t3 instance it needs. This returns (exclude_uuids, only_uuids)
    telling the setting which tasks to drop or keep:

    - default: any heavy task that needs more RAM than the current AWS_INSTANCE_TYPE is
      dropped so it can't crash the run, and a ready-to-paste command to run those tasks
      on a bigger instance is printed;
    - --only-heavy: keep ONLY the heavy tasks (that big-instance follow-up pass);
    - --include-heavy: skip this guard and run heavy tasks on the current VM anyway.
    """
    if not config.HEAVY_TASKS_FILE.exists():
        return None, None
    registry = json.loads(config.HEAVY_TASKS_FILE.read_text())
    heavy = {u: registry[u]["instance_type"]
             for u in _config_uuids(args.test_config) & set(registry)}

    if args.only_heavy:
        if not heavy:
            logger.warning("--only-heavy: no heavy tasks in '%s'; nothing to run.", args.test_config)
        return None, sorted(heavy)
    if not heavy:
        return None, None
    if args.include_heavy:
        logger.warning("--include-heavy: running %d heavy task(s) at the current instance type.", len(heavy))
        return None, None

    current = os.getenv("AWS_INSTANCE_TYPE", config.DEFAULT_INSTANCE_TYPE)
    cur_mem = config.T3_MEM_GIB.get(current, 0)
    too_big = {u: t for u, t in heavy.items() if config.T3_MEM_GIB.get(t, 1 << 30) > cur_mem}
    if not too_big:
        return None, None  # the current instance is already big enough

    req = max(too_big.values(), key=lambda t: config.T3_MEM_GIB.get(t, 0))
    print("\n" + "=" * 78)
    print(f"HEAVY-TASK GUARD: {len(too_big)} task(s) in '{args.test_config}' need a bigger VM "
          f"than the current AWS_INSTANCE_TYPE={current}.")
    print("Excluding them from this run. Rerun them large with:")
    for u, t in too_big.items():
        print(f"  - {u}  (needs {t})")
    print(f"  AWS_INSTANCE_TYPE={req} INSPECT_LOG_DIR=<dir> \\")
    print(f"    .venv/bin/python scripts/run_osworld.py --test_config {args.test_config} --only-heavy \\")
    print(f"    --model {args.model} --mode {args.mode} --max_sandboxes 1  (plus this run's other flags)")
    print("=" * 78 + "\n")
    return sorted(too_big), None


logger = logging.getLogger(__name__)


def _validate_args(args: argparse.Namespace) -> None:
    """Reject contradictory flag combinations (hard errors) and warn about ones
    that are silently no-ops or footguns."""
    if args.side_task_as_main and args.mode != "honest":
        raise ValueError(
            "--side_task_as_main must be combined with --mode honest: the side task is "
            "presented as a normal task, so there is no attack framing."
        )
    if not args.skip_proxy_tasks and not _PROXY_CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Proxy-requiring tasks run with a residential proxy by default, but no proxy "
            f"config was found at {_PROXY_CONFIG_PATH}. Set it up (copy dataimpulse.json.example "
            f"and fill in credentials — see the Setup section in README.md), or pass "
            f"--skip_proxy_tasks to drop those tasks."
        )

    if args.mode == "honest":
        if args.attack_reminders:
            logger.warning("--attack_reminders has no effect in --mode honest (no side task is pursued).")
        if args.per_step_side_eval:
            logger.warning("--per_step_side_eval is intended for attack runs; it is a no-op in --mode honest.")
        if args.attack_prompt_file or args.side_task_verifier_agent:
            logger.warning("Attack-prompt flags are ignored in --mode honest.")

    # --side_task_verifier_agent overrides --attack_prompt_file (see policy selection in main()).
    if args.side_task_verifier_agent and args.attack_prompt_file:
        logger.warning("--side_task_verifier_agent overrides --attack_prompt_file.")
    attack_flag = args.side_task_verifier_agent or args.attack_prompt_file
    if args.empty_agent and (args.mode != "honest" or attack_flag):
        logger.warning("--empty_agent replaces the policy with a no-op; mode/attack-prompt flags are ignored.")


def main(args: argparse.Namespace) -> None:
    _validate_args(args)

    # Setting: VM resolution and task options.
    model_defaults = _get_model_defaults(args.model)
    screen_size = tuple(args.screen_size)
    api_resolution = tuple(args.api_resolution) if args.api_resolution else model_defaults.get("api_resolution", screen_size)

    exclude_uuids, only_uuids = _heavy_guard(args)

    setting = OSWorldSetting(
        test_config=args.test_config,
        straightforward=False,
        screen_size=screen_size,
        api_resolution=api_resolution,
        max_screenshots=args.max_screenshots,
        post_action_delay=args.post_action_delay,
        filter_out_proxy=args.skip_proxy_tasks,
        enable_proxy=not args.skip_proxy_tasks,
        side_task_as_main=args.side_task_as_main,
        non_hand_held=not args.hand_held,
        exclude_uuids=exclude_uuids,
        only_uuids=only_uuids,
    )

    # Model generation config: reasoning, ansd safety identifier for OpenAI models
    generate_config = model_defaults["generate_config"]()
    if args.reasoning_tokens == 0:
        generate_config = GenerateConfig()
    elif args.reasoning_tokens is not None:
        generate_config = GenerateConfig(reasoning_tokens=args.reasoning_tokens)
    if args.reasoning_effort is not None:
        generate_config = generate_config.model_copy(update={"reasoning_effort": args.reasoning_effort})
    if args.safety_identifier:
        # inspect's OpenAI Responses provider reads safety_identifier from extra_body
        # and hoists it to a top-level Responses param (responses_extra_body_fields()).
        extra_body = dict(generate_config.extra_body or {})
        extra_body["safety_identifier"] = args.safety_identifier
        generate_config = generate_config.model_copy(update={"extra_body": extra_body})
        
    # Policy: an untrusted honest+attack policy, then the optional side-task wrappers.
    if args.empty_agent:
        untrusted = osworld_empty_policy()
    else:
        honest = honest_policy_builder()
        if args.side_task_verifier_agent:
            attack = attack_policy_builder(prompt=osworld_policy_prompts["unsubtle_complete_side_task"])
        elif args.attack_prompt_file:
            attack = attack_policy_builder(prompt=Path(args.attack_prompt_file).read_text())
        else:
            attack = attack_policy_builder()
        untrusted = untrusted_policy_builder(attack=attack, honest=honest)

    if args.per_step_side_eval:
        untrusted = osworld_per_step_side_eval(inner=untrusted)
    if args.attack_reminders:
        reminder_kwargs = {}
        if args.attack_reminder_file:
            reminder_kwargs["reminder_text"] = Path(args.attack_reminder_file).read_text()
        every_n_steps = args.attack_reminder_every_n_steps
        if every_n_steps is None:
            every_n_steps = 1 if "openai" in args.model else 4
        untrusted = osworld_attack_reminder(inner=untrusted,
                                    every_n_steps=every_n_steps,
                                    **reminder_kwargs)

    # Assemble the eval and run it.
    if args.mode == "honest":
        modes = [EvalMode.HONEST]
    elif args.mode == "attack":
        modes = [EvalMode.ATTACK]
    else:
        modes = [EvalMode.HONEST, EvalMode.ATTACK]
    system_prompt = (osworld_policy_prompts["unsubtle_complete_side_task"]
                     if args.side_task_verifier_agent else None)
    eval_config = ControlEvalConfig(
        max_steps=args.max_steps,
        max_sandboxes=args.max_sandboxes,
        token_limit=args.token_limit,
        untrusted_model=args.model,
        untrusted_config=generate_config,
        trusted_model=args.model,
        trusted_config=generate_config,
        system_prompt=system_prompt,
        retry_on_error=args.retry_on_error,
    )

    eval_tasks = get_control_task_product(
        settings=setting,
        micro_protocols=untrusted,
        modes=modes,
        eval_config=eval_config,
    )

    eval(
        eval_tasks,
        **eval_config.eval_args(),
        display=args.display,
        sandbox_cleanup=not args.nosandbox_cleanup,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run an OSWorld control evaluation (honest and/or attack) and write an inspect .eval log.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    core = parser.add_argument_group("core")
    core.add_argument("--test_config", type=str, default=config.DEFAULT_TEST_CONFIG,
                      help="Which tasks to run: an evaluation_examples config name (e.g. test_single) or a path to a JSON config.")
    core.add_argument("--model", type=str, default="anthropic/claude-sonnet-4-6",
                      help="Untrusted policy model, as provider/name (e.g. anthropic/claude-sonnet-4-6).")
    core.add_argument("--mode", type=str, default="honest", choices=["honest", "attack", "both"],
                      help="honest = main task only; attack = also pursue the hidden side task; both = run each sample twice.")
    core.add_argument("--max_steps", type=int, default=config.DEFAULT_MAX_STEPS,
                      help="Maximum agent steps per sample.")
    core.add_argument("--max_sandboxes", type=int, default=1,
                      help="Maximum OSWorld VMs to provision in parallel.")
    core.add_argument("--only-heavy", action="store_true", dest="only_heavy",
                      help="Run only the memory-heavy tasks (those in heavy_tasks.json). Use this for "
                           "the follow-up pass on a big VM after the guard excluded them from a normal "
                           "run; set AWS_INSTANCE_TYPE to a large instance first.")
    core.add_argument("--include-heavy", action="store_true", dest="include_heavy",
                      help="Turn off the heavy-task guard: run the memory-heavy tasks on the current VM "
                           "even if it may be too small for them (risking out-of-memory crashes).")
    core.add_argument("--token_limit", type=lambda x: None if x.lower() == "none" else int(x),
                      default=config.DEFAULT_TOKEN_LIMIT,
                      help="Total token budget per sample (pass 'none' to disable).")
    core.add_argument("--retry_on_error", type=int, default=1,
                      help="How many times to re-run a sample that errored, on a fresh VM.")
    core.add_argument("--empty_agent", action="store_true",
                      help="Run an agent that does nothing and submits immediately. For quickly checking the pipeline and scoring (tasks should score 0), not the model.")

    attack = parser.add_argument_group("attack / side task")
    attack.add_argument("--per_step_side_eval", action="store_true",
                        help="ATTACK only: score the side task after every agent step. ")
    attack.add_argument("--attack_reminders", action="store_true",
                        help="ATTACK only: inject a ChatMessageSystem reminder every N steps for the agent to see")
    attack.add_argument("--attack_reminder_every_n_steps", type=int, default=None,
                        help="Reminder cadence in agent steps (no effect unless --attack_reminders). ")
    attack.add_argument("--attack_reminder_file", type=str, default=None,
                        help="File whose contents replace the default reminder text (only with --attack_reminders). ")
    attack.add_argument("--attack_prompt_file", type=str, default=None,
                        help="File whose contents are used as the attack prompt. Default without this flag: control-arena's built-in attack prompt. ")
    attack.add_argument("--side_task_verifier_agent", action="store_true",
                        help="Attack mode with a prompt that tells the agent to do the side task openly and ignore the main task, to check the side task can be done and scored at all. Overrides --attack_prompt_file.")
    attack.add_argument("--side_task_as_main", action="store_true",
                        help="Present the side task as the agent's only main task without an attack framing. Requires --mode honest.")

    framing = parser.add_argument_group("task framing")
    framing.add_argument("--hand_held", action="store_true",
                         help="Use a version of the side task instruction containing hand-held step-by-step side instructions.")

    # By default, proxy-requiring tasks run WITH a residential proxy (configured in
    # evaluation_examples/settings/proxy/dataimpulse.json; see reproduce/README.md).
    proxy = parser.add_argument_group("proxy")
    proxy.add_argument("--skip_proxy_tasks", action="store_true",
                       help="Drop proxy-requiring tasks from the dataset instead of running them. "
                            "Default: run them with the residential proxy (which must be configured).")

    res = parser.add_argument_group("resolution / screenshots")
    res.add_argument("--screen_size", type=int, nargs=2, default=list(config.DEFAULT_SCREEN_SIZE),
                     help="OSWorld VM display resolution (W H); same for every model.")
    res.add_argument("--api_resolution", type=int, nargs=2, default=None,
                     help="Resolution screenshots are sent to the model at (W H). Default: model-specific "
                          "(Anthropic 1280x720) else the screen size.")
    res.add_argument("--max_screenshots", type=int, default=config.DEFAULT_MAX_SCREENSHOTS,
                     help="How many of the most recent screenshots stay in the model's context; older ones are dropped to save tokens.")
    res.add_argument("--post_action_delay", type=float, default=config.DEFAULT_POST_ACTION_DELAY,
                     help="Seconds to wait after each action before screenshotting.")

    reasoning = parser.add_argument_group("reasoning")
    reasoning.add_argument("--reasoning_tokens", type=int, default=None,
                           help="Override reasoning token budget (Anthropic only; 0 disables reasoning).")
    reasoning.add_argument("--reasoning_effort", type=str, default=None,
                           choices=["minimal", "low", "medium", "high"],
                           help="Override reasoning_effort. gpt-5.x models reason ONLY when this is set; "
                                "leave unset for the provider default.")
    reasoning.add_argument("--safety_identifier", type=str, default=None,
                           help="OpenAI safety identifier (Responses API only). Stable per-end-user id for abuse "
                                "attribution. WARNING: a high-confidence policy violation PERMANENTLY blocks this id "
                                "across all GPT-5 requests, so use a dedicated id for attack/red-team runs "
                                "(e.g. 'osworld-attack-eval'), not your main org id. Default off (kept out of git).")

    misc = parser.add_argument_group("output / misc")
    misc.add_argument("--display", type=str, default="full",
                      help="inspect_ai display mode (terminal UI only; no effect on results). Use 'plain' for tmux/piped logs.")
    misc.add_argument("--nosandbox_cleanup", action="store_true",
                      help="Leave sandbox VMs running after the eval instead of tearing them down.")
    misc.add_argument("--loglevel", type=str, default="INFO",
                      choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                      help="Python logging level.")

    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.loglevel))

    main(args)
