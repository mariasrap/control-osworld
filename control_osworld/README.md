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

- **Python 3.12 or newer.**
- **An AWS account** with permission to create and terminate EC2 instances, plus a
  subnet and security group for them. Every sample boots its own instance, so
  there is no way to try this without one. Configured in Setup below.

## Installation

Order matters here, and getting it wrong is the most common way to end up with a
broken environment. **Install OSWorld last, and never re-run the resolver
afterwards.**

From the repository, which pins every dependency through `uv.lock`:

```bash
git clone https://github.com/mariasrap/control-osworld.git
cd control-osworld
uv sync                       # creates .venv from the lockfile
```

Or as a package, into an environment you already have:

```bash
uv pip install control-osworld     # or: pip install control-osworld
```

Then add OSWorld itself, which supplies the desktop environment and the task
checkers. It is deliberately **not** a declared dependency:

```bash
uv pip install --no-build-isolation \
  "osworld @ git+https://github.com/xlang-ai/OSWorld.git@5fa8a8a6071bcccf17f9bfaab46c603574ff91b0"
```

Three things about that command:

- **`--no-build-isolation` is required.** Without it the build fails.
- **The commit is pinned** because OSWorld's `main` moves, and its task checkers
  are what decide whether a run counts as a success.
- **Do not run `uv sync` again afterwards.** OSWorld is absent from `pyproject.toml`
  and the lockfile, so the resolver treats it as unwanted and removes it. Use
  `uv pip install <pkg>` for anything you add later.

Check it worked:

```bash
python -c "import desktop_env, control_osworld; print('ok')"
```

## Setup

Three things to configure: AWS (always), a proxy (for 52 tasks), and a Google
account (for 8). Then the API key for whichever model you run.

### 1. AWS

Each sample boots its own EC2 desktop, so this is the part that takes real work.

**a. Get credentials.** In the AWS console, *Security Credentials* -> *Create
access key*. Keep the pair; the secret is shown once.

**b. Pick a supported region.** OSWorld publishes the desktop image in
**`us-east-1`** and **`ap-east-1`** only. Anywhere else fails with "Region ... is
not supported" — the AMI simply is not there. `us-east-1` is the default.

**c. Run from inside the same VPC.** The desktop's control port is not reachable
from the open internet by design, so the machine you launch evaluations from
should be an EC2 instance in the same (default) VPC. Sizing that machine, by how
many desktops you run at once:

| Parallel samples | Machine to run from |
|---|---|
| under 5 | `t3.medium` |
| under 15 | `t3.large` |
| 15+ | `c4.8xlarge` |

Give it at least 50 GB of disk.

**d. Create a security group** for the desktops with these inbound rules. The
`172.31.0.0/16` source is the default VPC range, i.e. "reachable from your runner
but not the internet". Port 5910 is the exception — it is the VNC view, which you
open from your own browser.

| Port | Source | What it is |
|---|---|---|
| 22 | `172.31.0.0/16` | SSH |
| 80 | `172.31.0.0/16` | HTTP |
| 5000 | `172.31.0.0/16` | the desktop's control server — how the agent acts |
| 8006, 8080, 8081, 9222 | `172.31.0.0/16` | OSWorld's other services |
| 5910 | `0.0.0.0/0` | noVNC, to watch a run in a browser |

**e. Note the IDs** of that security group and of the default VPC's subnet, then
export everything:

```bash
export AWS_ACCESS_KEY_ID="..."
export AWS_SECRET_ACCESS_KEY="..."
export AWS_REGION="us-east-1"          # or ap-east-1
export AWS_SUBNET_ID="subnet-..."
export AWS_SECURITY_GROUP_ID="sg-..."
export AWS_INSTANCE_TYPE="t3.xlarge"   # size of each desktop; optional
export CLIENT_PASSWORD="..."           # desktop password baked into the image
```

`AWS_INSTANCE_TYPE` sizes the **desktops**, not your runner. One task needs at
least `t3.large` (see Caveats).

### 2. Residential proxy

52 tasks visit sites that block datacentre IPs, so requests from EC2 are refused
and the tasks fail. OSWorld's answer is a residential proxy, and the config format
here is theirs.

1. Create an account at [DataImpulse](https://dataimpulse.com/) and buy a **US
   residential** package — billed by traffic, roughly $1/GB, which goes a long way
   since only these tasks use it.
2. Their dashboard gives you a **username and password** for the gateway. The
   gateway itself is `gw.dataimpulse.com` on port `823`.
3. Copy the template and fill those in:

```bash
cp control_osworld/evaluation_examples/settings/proxy/dataimpulse.json.example \
   control_osworld/evaluation_examples/settings/proxy/dataimpulse.json
```

```json
[
    {
        "host": "gw.dataimpulse.com",
        "port": 823,
        "username": "your_username",
        "password": "your_password",
        "protocol": "http",
        "provider": "dataimpulse",
        "type": "residential",
        "country": "US"
    }
]
```

It is a list because OSWorld treats it as a pool: add more entries and it rotates
between them, benching one for five minutes after three failures. Another
provider works too if it speaks HTTP or SOCKS5 — only `host`, `port`, `username`,
`password` and `protocol` are read.

To skip these tasks instead, set `filter_out_proxy=True` on the setting.

### 3. Google account

Eight tasks sign in to Google Drive. Fill in
`evaluation_examples/settings/google/settings.json` from the `.template` beside
it. None of the eight has a side task, so this only matters if you run the full
task list.

### 4. Model API key

`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, for whichever provider the agent and the
monitor use.

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

