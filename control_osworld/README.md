# Control OSWorld Setting

The OSWorld setting for Control Arena. Tasks are real jobs on a real Ubuntu
desktop — edit a spreadsheet, fix a slide deck, reorganise files — and the agent
does them the way a person would, by looking at screenshots and clicking and
typing. Each task is paired with a **side task**: a harmful objective (exfiltrate
a file, weaken a setting, plant persistence) that an attacking agent is secretly
told to pursue while it does the visible job. Main and side are scored
independently, so a run can succeed at one, both, or neither.

This package is a library. Command-line runners for it live in the [`scripts/`](https://github.com/mariasrap/control-osworld/tree/main/scripts) folder of the repository.

## Quick Start

Installing the package registers the setting with Control Arena, so its CLI can
run it by name:

```bash
control-arena eval single --setting osworld
```

Or assemble it yourself:

```python
from control_osworld import OSWorldSetting

setting = OSWorldSetting(test_config="test_small")
```

`test_config` names a file in `evaluation_examples/` listing which tasks to
include. Fuller runners -- attack prompts, monitors, classifiers -- are in
[`scripts/`](https://github.com/mariasrap/control-osworld/tree/main/scripts).

Complete the Setup below first: this setting needs cloud credentials and boots a
real machine per sample.

## Prerequisites

- **An AWS account**, with permission to create and terminate EC2 instances, and a
  subnet and security group for them. Every sample boots its own instance.
- **The OSWorld AMI** in that region, holding the Ubuntu desktop and its server.
  `snapshot_name` selects it.
- **OSWorld installed** so `desktop_env` imports. Install it with
  `--no-build-isolation`, keep it out of `pyproject.toml`, and do not run
  `uv sync` afterwards — any of those will break the install.

## Setup

**Credentials.** Export these, or keep them in a file you source before running.

| Variable | Purpose |
|---|---|
| `AWS_REGION` | Region the sandbox VMs are launched in |
| `AWS_SUBNET_ID`, `AWS_SECURITY_GROUP_ID` | Where in your VPC they are placed |
| `AWS_INSTANCE_TYPE` | VM size; optional, defaults to `t3.xlarge` |
| `CLIENT_PASSWORD` | Desktop password baked into the VM image |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Whichever provider the agent and monitor use |

**Proxy.** 52 tasks reach sites that block datacentre IPs and need a residential
proxy. Copy the template beside it and fill in your account:

```bash
cp control_osworld/evaluation_examples/settings/proxy/dataimpulse.json.example \
   control_osworld/evaluation_examples/settings/proxy/dataimpulse.json
```

Without it those tasks fail. Set `filter_out_proxy=True` on the setting to drop
them instead.

**Google Drive.** Eight tasks need a live Google account, configured the same way
from `evaluation_examples/settings/google/settings.json.template`. None of them
has a side task, so they matter only if you run the full task list.

## What a run does

1. `dataset.py` reads the task's JSON and its side task's JSON, merges their setup
   steps, and builds one sample carrying both.
2. `osworld_aws_sandbox.py` boots an EC2 instance from an AMI with the desktop and
   a Flask server on it, then applies the task's setup to that machine.
3. `osworld_setting.py` sets the screen resolution and shows the agent a first
   screenshot.
4. The agent acts through the computer tool (`computer_tool/`), one screenshot per
   step, optionally watched by a monitor (`monitors/`).
5. `scorers.py` runs OSWorld's own checker against the machine — once for the main
   task, again for the side task — and the VM is torn down.

## Layout

| Path | What it is |
|---|---|
| `osworld_setting.py` | The `Setting` Control Arena consumes: assembles dataset, sandbox, tools and scorers |
| `dataset.py` | Turns task JSON into samples; pairs main with side task |
| `scorers.py` | Runs OSWorld's checkers; also the per-step side-task scorer |
| `osworld_aws_sandbox.py` | The EC2 VM: boot, run actions on it, tear down |
| `config.py` | Defaults and paths, in one place |
| `computer_tool/` | The agent's hands: screenshot/click/type, and the script deployed to the VM |
| `policy/` | Agents: no-op, human-over-VNC, and the attack-reminder wrapper |
| `monitors/` | Trusted monitors that score a run for suspicion |
| `evaluation_examples/` | The task data. See its own README |
| `tests/` | Sandbox integration tests |

## The setting

`OSWorldSetting` is what Control Arena is handed. Its fields select the tasks
(`test_config`), the display (`screen_size`, `api_resolution`, `max_screenshots`),
and the run mode (`side_task_as_main`, `non_hand_held`, `enable_proxy`).

Defaults live in `config.py` and nowhere else — the setting's fields and
the [`scripts/`](https://github.com/mariasrap/control-osworld/tree/main/scripts) runners' command-line defaults both read
from it, so they cannot drift apart.

## Tasks and scoring

A main task is an OSWorld task, unchanged in most cases. Its side task is a second
JSON next to it whose setup steps run first (so the folders and files it needs
exist) and whose checker runs separately at the end.

- **Two wordings.** Every side task ships a step-by-step instruction and a terse
  one; `non_hand_held` picks which the agent gets.
- **Infeasible tasks.** Some OSWorld tasks are impossible on purpose. There is
  nothing to check on the machine, so those are scored on whether the agent said
  so instead.
- **Per-step scoring.** `osworld_per_step_side_eval` scores the side task after
  every step rather than only at the end, which shows *when* it was achieved. It
  is written as an agent wrapping the policy, because that is the only thing that
  runs between steps.

## Policies and monitors

Policies (`policy/`): `osworld_empty_policy` submits immediately and does nothing,
which is the control for the pipeline itself; `osworld_human_policy` hands the
desktop to a person over noVNC for baselining; `osworld_attack_reminder` wraps the
attacking policy and re-injects its instructions every few steps.

Monitors (`monitors/`) come in two kinds:

| | Calls | Use |
|---|---|---|
| `osworld_full_trajectory_monitor` | one per run | a single suspicion score for the whole run |
| `osworld_per_step_monitor` | one per step | shows when in the run suspicion rose |

The per-step monitor takes `mode` (`rolling` carries the conversation forward,
`independent` scores each step alone) and `score_point` (`post_action` lets it see
what the action did, `pre_action` does not — only the latter is available to a
monitor watching live).

Both take a `variant` setting what they are shown. The agent's private reasoning
is never shown in any of them.

| Variant | Screenshots | Agent's visible text |
|---|---|---|
| `no_text` | yes | no |
| `with_text` | yes | yes |
| `no_images` | no | yes |

## The sandbox

An Inspect `SandboxEnvironment` wrapping
OSWorld's `DesktopEnv`, talking to a Flask server on the instance over HTTP.

```
┌─────────────────────┐     HTTP      ┌─────────────────────┐
│  Inspect Sandbox    │ ───────────── │  EC2 Instance       │
│  (OSWorldSandbox)   │   port 5000   │  (Flask Server)     │
└─────────────────────┘               └─────────────────────┘
         │                                      │
         │ wraps                                │ runs
         ▼                                      ▼
┌─────────────────────┐               ┌─────────────────────┐
│  OSWorld DesktopEnv │               │  Ubuntu Desktop     │
│  (VM Management)    │               │  + pyautogui        │
└─────────────────────┘               └─────────────────────┘
```

### Methods

```python
# Inspect's SandboxEnvironment interface
result = await sandbox.exec(["echo", "hello"], cwd="/tmp", env={"FOO": "bar"})
contents = await sandbox.read_file("/etc/hostname")
await sandbox.write_file("/tmp/test.txt", "hello world")

# OSWorld-specific
await sandbox.launch(["google-chrome", "--no-sandbox", "https://example.com"])
screenshot = await sandbox.get_screenshot()          # PNG bytes
a11y_tree = await sandbox.get_accessibility_tree()   # XML
await sandbox.execute_pyautogui("pyautogui.click(100, 200)")

sandbox.vm_ip        # instance private IP
sandbox.instance_id  # EC2 instance id
sandbox.env          # the underlying DesktopEnv
```

`exec()` waits for the command and returns its output, and the server gives up
after 120 s. `launch()` is fire-and-forget — use it for GUI apps like Chrome that
never exit, since `exec()` would block until the timeout.

### Configuration

```python
from control_osworld.osworld_aws_sandbox import OSWorldSandboxConfig

config = OSWorldSandboxConfig(
    provider_name="aws",
    region=None,                   # defaults to $AWS_REGION
    snapshot_name="init_state",    # the AMI
    screen_size=(1920, 1080),      # the VM's real resolution
    api_resolution=(1920, 1080),   # what screenshots are downscaled to
    post_action_delay=0.5,
    headless=False,
    require_a11y_tree=True,
    require_terminal=False,
    server_startup_timeout=180,
    enable_proxy=False,
)
```

### Server endpoints

What the Flask server on the instance exposes, and how each expects its data.

| Endpoint | Method | Data | Returns |
|---|---|---|---|
| `/platform` | GET | – | OS platform, e.g. "Linux" |
| `/screenshot` | GET | – | PNG screenshot, cursor included |
| `/accessibility` | GET | – | accessibility tree as XML |
| `/terminal` | GET | – | terminal output, if one is open |
| `/cursor_position` | GET | – | `[x, y]` |
| `/screen_size` | POST | – | `{width, height}` |
| `/desktop_path` | POST | – | desktop directory path |
| `/wallpaper` | POST | – | wallpaper image |
| `/execute` | POST | JSON `{command, shell}` | `{status, output, error, returncode}` |
| `/setup/execute` | POST | JSON `{command, shell}` | as `/execute` |
| `/setup/launch` | POST | JSON `{command, shell}` | fire-and-forget launch |
| `/setup/upload` | POST | form `file_path`, `file_data` | upload a file |
| `/file` | POST | form `file_path` | download a file |
| `/list_directory` | POST | JSON `{path}` | directory contents |

Note the inconsistency: reads and uploads take **form data**, everything else
takes JSON. `/run_bash_script` and `/run_python` return 404 on this AMI — use
`/execute` with `shell=True`.

### Things that will bite you

- **The instance is "running" before the server is.** The sandbox polls
  `/platform` until it answers, up to `server_startup_timeout`.
- **Signal handlers only work on the main thread.** OSWorld's AWS provider
  registers them unconditionally, which throws when the sandbox runs off-thread.
  We patch its `providers/aws/manager.py` to check it is on the main thread first.
- **`exec()` has a 120 s server-side limit.** Anything longer, or anything that
  never exits, needs `launch()`.

## Tests

`tests/test_osworld_sandbox.py` covers the sandbox against a real instance: that
it reaches "running", returns a valid PNG, runs commands with the right exit codes
and environment, and terminates afterwards. It **creates real EC2 instances** and
is skipped in CI.

```bash
.venv/bin/pytest -v -s control_osworld/tests/test_osworld_sandbox.py
```

## Security warning

This setting deliberately asks agents to do harmful things: exfiltrate files,
weaken security settings, plant persistence. The side tasks are real actions on a
real machine, not simulated ones, and that machine has internet access and,
for some tasks, a residential proxy.

The VM isolates the agent from your machine, and nothing more. Treat each instance
as untrusted for as long as it lives: give it no credentials beyond what the task
needs, keep it in a subnet you are willing to have an agent act from, and do not
reuse an instance after a run. The sandboxing is not sufficient for an agent that
is both capable and independently malicious.

## Caveats and limitations

- **One patch lives outside this package.** OSWorld's AWS provider registers signal
  handlers unconditionally, which throws when the sandbox runs off the main
  thread, so we patch its `providers/aws/manager.py` in place. Reinstalling
  OSWorld reverts it, and the symptom is the first entry under Common issues.
- **Runs cost real money and real time.** Every sample boots an EC2 instance and
  waits on a desktop; VM setup alone is a couple of minutes before the agent acts.
- **Not every task can be attacked.** 318 of the 369 main tasks have a side task.
  The other 51 either need a live Google account, are meant to be answered
  "impossible", or cannot be done through the GUI — none of which leaves a
  trajectory a side task could hide inside.
- **52 tasks need a residential proxy** and fail without one; `filter_out_proxy`
  drops them instead.
- **One task needs a bigger VM.** It exhausts the default instance's memory; it is
  listed in `evaluation_examples/heavy_tasks.json`, and the runners in
  [`scripts/`](https://github.com/mariasrap/control-osworld/tree/main/scripts) keep it out of a normal run.

## Common issues

| Symptom | Cause |
|---|---|
| `signal only works in main thread` | The OSWorld signal-handler patch above is missing |
| Connection refused on port 5000 | The server is still starting; `sample_init` normally waits for it |
| 500 from `/execute` | The command timed out at 120 s, or failed. For GUI apps use `launch()` |
| 404 from `/run_bash_script` | Not on this AMI; use `/execute` with `shell=True` |
