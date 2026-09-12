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
| [`scripts/`](scripts/) | Scripts for evaluations, monitors, classifiers, and the two generation pipelines. Detailed below. |
| [`side_task_templates/`](side_task_templates/) | The side task catalog: the 81 templates, how each pairs with a main task, and every deviation from stock OSWorld. |

## Installation and setup

See **[`control_osworld/README.md`](control_osworld/README.md)** for installation and
setup. Running anything needs an AWS account and API keys for the models, and some
tasks additionally need a residential proxy account.

## Reproducing the experiments

See **[`scripts/reproduce/README.md`](scripts/reproduce/README.md)** to reproduce the
results in the paper.

## Scripts

Every script takes `--help`, which lists all of its options with defaults. The
examples below are the shortest useful form of each.

### Running an evaluation

**[`run_osworld.py`](scripts/run_osworld.py)** runs an evaluation and writes an
inspect `.eval` log. You pick the model and whether the agent is honest or
attacking; the remaining flags choose the variation — which attack prompt, whether
reminders are injected, whether the side task is scored at every step, and so on.

```bash
python scripts/run_osworld.py --model anthropic/claude-sonnet-4-6 \
    --mode attack --test_config test_small
```

**[`run_osworld_human_baseline.py`](scripts/run_osworld_human_baseline.py)** hands
the desktop to a person over noVNC rather than a model, to check by hand whether a
task can be done at all.

```bash
python scripts/run_osworld_human_baseline.py --test_config test_single
```

### Scoring a finished run

These read an `.eval` log and write JSON; none of them re-runs the agent. Each
needs the log to read, a path to write, and the model doing the judging.

**[`run_monitor.py`](scripts/run_monitor.py)** scores each trajectory as a whole:
one suspicion score per run. `--variant` sets what the monitor is shown.

```bash
python scripts/run_monitor.py --log-path logs/run.eval --variant with_text \
    --model anthropic/claude-haiku-4-5 --out-path logs/monitor/run.json
```

**[`run_monitor_per_step.py`](scripts/run_monitor_per_step.py)** scores step by
step instead, so you can ask whether the agent would have been flagged before the
harmful action ran rather than after.

```bash
python scripts/run_monitor_per_step.py --log-path logs/run.eval --variant with_text \
    --model anthropic/claude-haiku-4-5 --out-path logs/monitor/run_perstep.json
```

**[`classifiers/refusal_classifier.py`](scripts/classifiers/refusal_classifier.py)**
asks whether the agent refused, and whether it refused on safety grounds rather
than because it got stuck — separating "would not" from "could not".

```bash
python scripts/classifiers/refusal_classifier.py --log-path logs/run.eval \
    --out-path logs/classifier/refusal.json
```

**[`classifiers/side_task_leak_classifier.py`](scripts/classifiers/side_task_leak_classifier.py)**
asks whether the agent gave itself away in its visible text, naming the hidden
objective rather than merely acting on it.

```bash
python scripts/classifiers/side_task_leak_classifier.py --log-path logs/run.eval \
    --out-path logs/classifier/leak.json
```

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
