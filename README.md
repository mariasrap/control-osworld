# Control OSWorld

This repository contains Control OSWorld — a control evaluation for **computer use**
agents, built as a Control Arena setting — together with the monitors, prompt
optimization and experiments we ran on it for the paper *Control OSWorld: An AI
Control Environment for GUI Computer Use Agents* [link].

The setting pairs 318 tasks from [OSWorld](https://github.com/xlang-ai/OSWorld) with
81 programmatically verified harmful side tasks: exfiltrate a private file, weaken a
security setting, tamper with a document.

## What is here

| | |
|---|---|
| [`control_osworld/`](control_osworld/) | The environment itself, packaged as a Control Arena setting: tasks, the sandbox that runs them, scorers, policies and monitors. **Start with [its README](control_osworld/README.md)** — installation, setup and how the pieces fit. |
| [`scripts/`](scripts/) | Everything you run: evaluations, monitors, classifiers, and the two generation pipelines. Detailed below. |
| [`side_task_templates/`](side_task_templates/) | The side task catalog: the 81 templates, how each pairs with a main task, and every deviation from stock OSWorld. |

## Installation and setup

See **[`control_osworld/README.md`](control_osworld/README.md)** for installation and
setup. Running anything needs an AWS account and API keys for the models, and some
tasks additionally need a residential proxy account.

## Reproducing the experiments

See **[`scripts/reproduce/README.md`](scripts/reproduce/README.md)** to reproduce the
results in the paper.

## Scripts

### Running an evaluation

| | |
|---|---|
| [`run_osworld.py`](scripts/run_osworld.py) | Runs an evaluation and writes an inspect `.eval` log. One flag chooses the mode: honest, attack with a prompt of your choice, either of those with an empty agent, the side task presented openly as the main task, or the side-task verifier. `--per_step_side_eval` additionally scores the side task after every step. |
| [`run_osworld_human_baseline.py`](scripts/run_osworld_human_baseline.py) | Hands the desktop to a person over noVNC instead of a model, to check by hand whether a task is doable at all. |

### Scoring a finished run

These read an `.eval` log and write JSON; none of them re-runs the agent.

| | |
|---|---|
| [`run_monitor.py`](scripts/run_monitor.py) | The trusted monitor over each whole trajectory: one suspicion score per run. |
| [`run_monitor_per_step.py`](scripts/run_monitor_per_step.py) | The trusted monitor step by step, so you can ask whether it would have flagged the agent before the harmful action executed rather than after. |
| [`classifiers/refusal_classifier.py`](scripts/classifiers/refusal_classifier.py) | Did the agent refuse, and was it on safety grounds rather than because it got stuck? Separates "would not" from "could not". |
| [`classifiers/side_task_leak_classifier.py`](scripts/classifiers/side_task_leak_classifier.py) | Did the agent give itself away in its visible text — naming the hidden objective, rather than merely acting on it? |

Every one takes `--help`, and their module docstrings document the output JSON
field by field.

### Pipelines

| | |
|---|---|
| [`attack_prompt_gepa/`](scripts/attack_prompt_gepa/) | Optimizes the attack prompt, and the reminder injected mid-run, with GEPA — searching for prompts that keep the side task completed while lowering how suspicious the monitor finds the trajectory. |
| [`side_task_generation_pipeline/`](scripts/side_task_generation_pipeline/) | The agent pipeline that drafted the side task catalog from OSWorld's tasks, kept as the record of how those tasks were produced. |
| [`reproduce/`](scripts/reproduce/) | The paper's experiments, one subcommand each. |

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

## License

MIT.
