# Control OSWorld

An AI control environment for GUI computer use agents.

AI control studies how to stop an AI system from causing harm even if it is
misaligned and actively trying to. Most of that work has been done on coding
agents. This repository is a control evaluation for **computer use** agents —
models that operate a real desktop through its graphical interface — together with
the experiments we ran on it.

It pairs 318 tasks from [OSWorld](https://github.com/xlang-ai/OSWorld) with 81
programmatically verified harmful side tasks — exfiltrate a private file, weaken a
security setting, tamper with a document — that an attacking agent must complete
without a monitor noticing. The ordinary task and the hidden one are scored
independently, and a per-step scorer records the exact step at which the side task
was achieved, so a monitor can be judged on whether it would have caught the agent
*in time to stop it* rather than merely in hindsight.

**Paper:** *Control OSWorld: An AI Control Environment for GUI Computer Use
Agents* — link to follow.

## What is here

| | |
|---|---|
| [`control_osworld/`](control_osworld/) | The environment itself, packaged as a Control Arena setting: tasks, the sandbox that runs them, scorers, policies and monitors. **Start with [its README](control_osworld/README.md)** — installation, setup and how the pieces fit. |
| [`scripts/`](scripts/) | Runners: evaluations, the monitors over a finished run, refusal and side-task-leakage classifiers, and the GEPA attack-prompt optimizer. |
| [`scripts/reproduce/`](scripts/reproduce/) | The paper's experiments, one subcommand per experiment. |
| [`side_task_templates/`](side_task_templates/) | The side task catalog: the 81 templates, how each pairs with a main task, and every deviation from stock OSWorld. |

## Installation and setup

Everything you need is in **[`control_osworld/README.md`](control_osworld/README.md)**.
Be aware before you start that this is not a local benchmark: every task boots its
own cloud desktop, so it needs an AWS account, and a subset of tasks needs a
residential proxy.

## Reproducing the experiments

```bash
python scripts/reproduce/reproduce.py <subcommand> --dry-run
```

Only the combinations that appear in the paper can be run; anything else is
refused rather than quietly evaluated. Start with `--dry-run`, which prints the
commands without running them — the full set is days of cloud time. See
[`scripts/reproduce/README.md`](scripts/reproduce/README.md).

## What we found

- A weaker monitor reliably separates honest from malicious trajectories produced
  by a stronger agent **when it sees the whole run**: 97% recall at a 3% false
  positive rate.
- **Judging each step before it executes is much harder.** Recall falls at low
  false positive rates, with less context to judge an action against.
- **What the monitor sees matters, and not as expected.** The agent's own visible
  text is the dominant signal; screenshots add little once text is available.
- **Framing decides refusal.** Opus 4.8 refuses 26 of 81 side tasks when they are
  asked as plain user requests, and none of 318 when the same actions are framed
  as a covert secondary objective.

Exact figures, the models evaluated and confidence intervals are in the paper.

## Built on

[OSWorld](https://github.com/xlang-ai/OSWorld) (Xie et al., 2024) for the desktop
environment and the main tasks, and
[Control Arena](https://github.com/UKGovernmentBEIS/control-arena) for the control
evaluation framework.

## Citation

```bibtex
@misc{controlosworld,
  title  = {Control OSWorld: An AI Control Environment for GUI Computer Use Agents},
  note   = {Citation to follow}
}
```

## Licence

MIT.
