# GUI Control Arena

<!-- TODO: one-paragraph description of the project. -->

## Folder structure

- `control_osworld/` — the package: the OSWorld control setting, sandbox, dataset,
  policies, scorers, and monitors. See `control_osworld/README.md`.
- `scripts/` — command-line entry points and tools:
  - `run_osworld.py` — run an OSWorld control evaluation (honest and/or attack).
  - `run_osworld_human_baseline.py` — do a task by hand over VNC to check feasibility.
  - `run_monitor.py` / `run_monitor_per_step.py` — score an eval log with the monitor.
  - `attack_prompt_gepa/` — optimize the attack prompt with GEPA. See its README.
  - `side_task_generation_pipeline/` — generate side task templates. See its README.
  - `reproduce/` — reproduce the paper's runs. See `scripts/reproduce/README.md`.
  - `classifiers/` — refusal and side-task-leak classifiers.
- `side_task_templates/` — the side task catalog, pairings, and OSWorld deviations.
  See `side_task_templates/README.md`.

## Install

<!-- TODO: install steps.
     NOTE: OSWorld must be installed with --no-build-isolation, never added to
     pyproject.toml, and never followed by `uv sync` (it breaks the install). -->

## Setup

These must be configured before running anything.

**API keys & AWS credentials.** The runners read the following from the environment
(`export` them in your shell, or keep them in an env file and source it):

| variable | purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic policy + monitor calls (Sonnet / Opus / Haiku) |
| `OPENAI_API_KEY` | OpenAI policy + monitor calls (GPT-5.x) |
| `AWS_REGION` | region for the OSWorld sandbox VMs |
| `AWS_SUBNET_ID`, `AWS_SECURITY_GROUP_ID` | VPC placement for the VMs |
| `AWS_INSTANCE_TYPE` | VM size (e.g. `t3.medium`) |
| `CLIENT_PASSWORD` | VNC/agent password baked into the VM image |

**Proxy config.** A few tasks require a residential proxy, and by default the runner runs
them through it. Copy the example config and fill in your credentials:

```bash
cp control_osworld/evaluation_examples/settings/proxy/dataimpulse.json.example \
   control_osworld/evaluation_examples/settings/proxy/dataimpulse.json
# then edit in host/port/username/password
```

If the config is missing, `run_osworld.py` errors out before provisioning. Pass
`--skip_proxy_tasks` to drop those tasks instead of running them.

## Running experiments
<!-- TODO: fill in the how. -->
