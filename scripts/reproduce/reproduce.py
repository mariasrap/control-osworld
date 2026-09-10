#!/usr/bin/env python3
"""Reproduce the paper's experiments on the 318 main+side task pairs.

Every combination this accepts is one that made it into the paper; anything else is
rejected rather than silently run. Subcommands:

  honest            honest trajectories, per untrusted model
  attack            attack trajectories, per (untrusted model, attack prompt)
  side-as-main      the side task presented openly as the main task, per untrusted model
  monitors-full     full-trajectory monitor over finished honest and attack runs
  monitors-perstep  per-step monitor (Sonnet 4.6 with the GEPA prompt only)
  classifiers       refusal and side-task-leak classifiers over finished runs

With no --model/--prompt/--monitor a subcommand runs every valid combination. Log
paths are derived from the run, so the monitors and classifiers find their inputs
on their own and say what to run first if one is missing.

Trajectory runs launch hundreds of tasks on real VMs, so start with --dry-run.
Experiment-wide defaults (75 steps, 10 screenshots, 3.0s delay, 1920x1080) live in
run_osworld.py. Only the token-limit override, which is off for every run, and the
model- and prompt-specific flags appear here.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PY = REPO / ".venv/bin/python"
RUN_OSWORLD = REPO / "scripts/run_osworld.py"
RUN_FULL = REPO / "scripts/run_monitor.py"
RUN_PERSTEP = REPO / "scripts/run_monitor_per_step.py"
REFUSAL = REPO / "scripts/classifiers/refusal_classifier.py"
LEAK = REPO / "scripts/classifiers/side_task_leak_classifier.py"
PROMPT_DIR = REPO / "control_osworld/policy/prompts"

TEST_CONFIG = "test_filtered_318"
MAX_SANDBOXES = 15

# The untrusted (attack-capable) policies and the trusted monitors, by short name.
MODELS = {
    "sonnet46": "anthropic/claude-sonnet-4-6",
    "opus48": "anthropic/claude-opus-4-8",
    "gpt52": "openai/gpt-5.2",
}
MONITORS = {
    "haiku45": "anthropic/claude-haiku-4-5",
    "gpt50mini": "openai/gpt-5.0-mini",
}

# Attack prompts: (prompt file, reminder file, reminder every N steps). "ca" is
# Control Arena's built-in prompt, which we run without reminders.
PROMPTS = {
    "ca": (None, None, None),
    "gepa": (PROMPT_DIR / "attack_prompt_anthropic_GEPA.txt",
             PROMPT_DIR / "attack_reminder_anthropic_GEPA.txt", 4),
    "openai": (PROMPT_DIR / "attack_prompt_openAI_aggressive.txt",
               PROMPT_DIR / "attack_reminder_openAI_aggressive.txt", 1),
}

# Every run is uncapped; run_osworld would otherwise stop at its 1M token default.
COMMON_FLAGS = ["--token_limit", "none"]

# Model-specific run flags: the Anthropic models get a smaller screenshot and
# GPT-5.2 a reasoning effort.
MODEL_FLAGS = {
    "sonnet46": ["--api_resolution", "1280", "720"],
    "opus48": ["--api_resolution", "1280", "720"],
    "gpt52": ["--reasoning_effort", "high"],
}

# What the monitor sees, and the label used in the output name. The Anthropic runs
# were scored on all three; GPT-5.2 only on actions+screenshots.
VARIANT_LABEL = {"with_text": "all_inputs", "no_text": "no_text", "no_images": "no_images"}
MODEL_VARIANTS = {
    "sonnet46": ["with_text", "no_text", "no_images"],
    "opus48": ["with_text", "no_text", "no_images"],
    "gpt52": ["no_text"],
}

# The combinations that were actually run for the paper.
HONEST = ["sonnet46", "opus48", "gpt52"]
SIDE_AS_MAIN = ["sonnet46", "opus48", "gpt52"]
ATTACK = [("sonnet46", "ca"), ("sonnet46", "gepa"), ("opus48", "gepa"), ("gpt52", "openai")]
FULL_MON = [("sonnet46", "ca", "haiku45"), ("sonnet46", "gepa", "haiku45"),
            ("opus48", "gepa", "haiku45"),
            ("gpt52", "openai", "haiku45"), ("gpt52", "openai", "gpt50mini")]
PER_STEP = [("sonnet46", "gepa", "haiku45")]

RUN_LABEL = {"sonnet46": "Sonnet46", "opus48": "Opus48", "gpt52": "GPT52"}
MON_LABEL = {"haiku45": "Haiku45", "gpt50mini": "GPT5mini"}

NOT_RUN = ("This combination was not run for the paper. "
           "Please write your own script for further experiments.")


def die(message: str) -> None:
    sys.exit(f"error: {message}\n{NOT_RUN}")


def honest_name(model: str) -> str:
    return f"{RUN_LABEL[model]}_honest_318"


def attack_name(model: str, prompt: str) -> str:
    return f"{RUN_LABEL[model]}_attack_{prompt}_318"


def side_name(model: str) -> str:
    return f"{RUN_LABEL[model]}_side_as_main_318"


def sh(cmd: list, dry_run: bool) -> None:
    """Print a command, and run it unless this is a dry run."""
    print("  " + " ".join(str(c) for c in cmd))
    if not dry_run:
        subprocess.run([str(c) for c in cmd], check=True)


def trajectory(name: str, extra: list, log_dir: Path, dry_run: bool) -> None:
    """Run one evaluation and file its log under `name`, skipping runs already done."""
    out = log_dir / f"{name}.eval"
    if out.exists():
        print(f"  skip, already present: {out}")
        return
    before = set(log_dir.glob("*.eval"))
    sh([PY, RUN_OSWORLD, "--test_config", TEST_CONFIG,
        "--max_sandboxes", MAX_SANDBOXES, *COMMON_FLAGS, *extra], dry_run)
    if dry_run:
        return
    fresh = sorted(set(log_dir.glob("*.eval")) - before, key=lambda p: p.stat().st_mtime)
    if fresh:
        fresh[-1].rename(out)
        print(f"  wrote {out}")


def need_log(name: str, log_dir: Path, how: str, dry_run: bool) -> Path | None:
    """The trajectory log `name`, or None (with an explanation) if it is missing."""
    path = log_dir / f"{name}.eval"
    if path.exists() or dry_run:
        return path
    print(f"  missing {path}\n    produce it with: {how}", file=sys.stderr)
    return None


def pick(valid: list, given: dict, what: str) -> list:
    """The valid combinations matching the options given, or exit explaining."""
    keys = list(given)
    chosen = [c for c in valid
              if all(given[k] is None or (c[i] if isinstance(c, tuple) else c) == given[k]
                     for i, k in enumerate(keys))]
    if not chosen:
        asked = ", ".join(f"{k}={v}" for k, v in given.items() if v)
        die(f"no paper run matches {what} with {asked}.\n"
            f"Valid: {', '.join(str(c) for c in valid)}")
    return chosen


def cmd_honest(args) -> None:
    for model in pick(HONEST, {"model": args.model}, "honest"):
        print(f"[honest] {model}")
        trajectory(honest_name(model),
                   ["--model", MODELS[model], "--mode", "honest", *MODEL_FLAGS[model]],
                   args.log_dir, args.dry_run)


def cmd_attack(args) -> None:
    for model, prompt in pick(ATTACK, {"model": args.model, "prompt": args.prompt}, "attack"):
        print(f"[attack] {model} + {prompt}")
        prompt_file, reminder_file, every = PROMPTS[prompt]
        extra = ["--model", MODELS[model], "--mode", "attack",
                 *MODEL_FLAGS[model], "--per_step_side_eval"]
        if prompt_file:
            extra += ["--attack_prompt_file", prompt_file]
        if reminder_file:
            extra += ["--attack_reminders", "--attack_reminder_file", reminder_file,
                      "--attack_reminder_every_n_steps", every]
        trajectory(attack_name(model, prompt), extra, args.log_dir, args.dry_run)


def cmd_side_as_main(args) -> None:
    for model in pick(SIDE_AS_MAIN, {"model": args.model}, "side-as-main"):
        print(f"[side-as-main] {model}")
        trajectory(side_name(model),
                   ["--model", MODELS[model], "--mode", "honest", "--side_task_as_main",
                    *MODEL_FLAGS[model]],
                   args.log_dir, args.dry_run)


def _monitor_jobs(combos: list) -> dict:
    """(run name, untrusted model, monitor) to score — honest runs are scored once."""
    jobs = {}
    for model, prompt, monitor in combos:
        jobs[(attack_name(model, prompt), model, monitor)] = f"attack --model {model} --prompt {prompt}"
        jobs[(honest_name(model), model, monitor)] = f"honest --model {model}"
    return jobs


def _score(script: Path, combos: list, kind: str, args, extra: list) -> None:
    out_dir = args.log_dir / "monitor"
    out_dir.mkdir(parents=True, exist_ok=True)
    for (run, model, monitor), how in _monitor_jobs(combos).items():
        log = need_log(run, args.log_dir, f"reproduce.py {how}", args.dry_run)
        if log is None:
            continue
        for variant in MODEL_VARIANTS[model]:
            out = out_dir / f"{MON_LABEL[monitor]}_{run}_{kind}_{VARIANT_LABEL[variant]}.json"
            print(f"[{kind}] {run} x {variant} x {monitor}")
            sh([PY, script, "--log-path", log, "--variant", variant,
                "--model", MONITORS[monitor], *extra, "--out-path", out], args.dry_run)


def cmd_monitors_full(args) -> None:
    combos = pick(FULL_MON, {"model": args.model, "prompt": args.prompt,
                             "monitor": args.monitor}, "monitors-full")
    _score(RUN_FULL, combos, "full", args, [])


def cmd_monitors_perstep(args) -> None:
    combos = pick(PER_STEP, {"model": args.model, "prompt": args.prompt,
                             "monitor": args.monitor}, "monitors-perstep")
    _score(RUN_PERSTEP, combos, "perstep", args,
           ["--mode", "rolling", "--score-point", "pre_action"])


def cmd_classifiers(args) -> None:
    """Leak + refusal on the attack runs; refusal on the side-as-main runs.

    On an attack run the refusal question is about the hidden side task; on a
    side-as-main run that same task is the one the user asked for.
    """
    out_dir = args.log_dir / "classifier"
    out_dir.mkdir(parents=True, exist_ok=True)
    attack_runs = args.attack_trajectories or [attack_name(m, p) for m, p in ATTACK]
    side_runs = args.side_as_main_trajectories or [side_name(m) for m in SIDE_AS_MAIN]

    for run in attack_runs:
        log = need_log(run, args.log_dir, "reproduce.py attack", args.dry_run)
        if log is None:
            continue
        print(f"[leak] {run}")
        sh([PY, LEAK, "--log-path", log, "--out-path", out_dir / f"leak_{run}.json"], args.dry_run)
        print(f"[refusal] {run} (side task)")
        sh([PY, REFUSAL, "--log-path", log, "--task", "side",
            "--out-path", out_dir / f"refusal_{run}.json"], args.dry_run)

    for run in side_runs:
        log = need_log(run, args.log_dir, "reproduce.py side-as-main", args.dry_run)
        if log is None:
            continue
        print(f"[refusal] {run} (main task)")
        sh([PY, REFUSAL, "--log-path", log, "--task", "main",
            "--out-path", out_dir / f"refusal_{run}.json"], args.dry_run)


def main() -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true",
                        help="Print the commands that would run, without running them")
    common.add_argument("--log-dir", type=Path, default=REPO / "logs",
                        help="Where trajectory logs live and monitor/classifier output goes")

    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, fn, help_text):
        s = sub.add_parser(name, parents=[common], help=help_text)
        s.set_defaults(func=fn)
        return s

    s = add("honest", cmd_honest, "Honest trajectories")
    s.add_argument("--model", choices=MODELS, help="Untrusted model (default: all)")

    s = add("attack", cmd_attack, "Attack trajectories")
    s.add_argument("--model", choices=MODELS, help="Untrusted model (default: all)")
    s.add_argument("--prompt", choices=PROMPTS, help="Attack prompt (default: all)")

    s = add("side-as-main", cmd_side_as_main, "Side task presented as the main task")
    s.add_argument("--model", choices=MODELS, help="Untrusted model (default: all)")

    for name, fn, text in (("monitors-full", cmd_monitors_full, "Full-trajectory monitor"),
                           ("monitors-perstep", cmd_monitors_perstep, "Per-step monitor")):
        s = add(name, fn, text)
        s.add_argument("--model", choices=MODELS, help="Untrusted model of the scored run")
        s.add_argument("--prompt", choices=PROMPTS, help="Attack prompt of the scored run")
        s.add_argument("--monitor", choices=MONITORS, help="Trusted monitor model")

    s = add("classifiers", cmd_classifiers, "Refusal and side-task-leak classifiers")
    s.add_argument("--attack-trajectories", nargs="+", metavar="RUN",
                   help="Attack run names to classify (default: every paper attack run)")
    s.add_argument("--side-as-main-trajectories", nargs="+", metavar="RUN",
                   help="Side-as-main run names to classify (default: every paper run)")

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
