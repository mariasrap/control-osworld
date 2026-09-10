# Reproducibility

`reproduce.py` runs the paper's experiments or a subset selected through subcommands.
Only combinations leading to the paper's results can be run.

## Subcommands

| Subcommand | What it runs | Options |
|---|---|---|
| `honest` | Honest trajectories | `--model` |
| `attack` | Attack trajectories | `--model`, `--prompt` |
| `side-as-main` | The side task presented openly as the main task | `--model` |
| `monitors-full` | Full-trajectory monitor over the honest and attack runs | `--model`, `--prompt`, `--monitor` |
| `monitors-perstep` | Per-step monitor (rolling, scored pre-action) | `--model`, `--prompt`, `--monitor` |
| `classifiers` | Refusal and side-task-leak classifiers | `--attack-trajectories`, `--side-as-main-trajectories` |

Every subcommand also takes `--dry-run`, which prints the commands it would run
without running them. Recommended since the experiments are long and expensive, in both
compute and API tokens.

Omit the options to run every valid combination.

E.g.

```bash
reproduce.py honest                                  # all three untrusted models, honest mode
reproduce.py attack --model sonnet46 --prompt gepa   # one combination
reproduce.py monitors-full                           # every monitor on every honest and attack run
```

## What was run

**Untrusted models:** `sonnet46`, `opus48`, `gpt52`.
**Attack prompts:** `ca` (Control Arena's built-in, no reminders), `gepa`, `openai`.
**Trusted monitors:** `haiku45`, `gpt50mini`.

| | honest | attack | side-as-main |
|---|---|---|---|
| Sonnet 4.6 | ✓ | `ca`, `gepa` | ✓ |
| Opus 4.8 | ✓ | `gepa` | ✓ |
| GPT-5.2 | ✓ | `openai` | ✓ |

Monitors, by what the monitor sees (A=actions, T=text, S=screenshots):

| Scored run | Monitor | Full-trajectory | Per-step |
|---|---|---|---|
| Sonnet 4.6 (`ca`, `gepa`) | Haiku 4.5 | T+A+S, A+S, A+T | `gepa` only, same three |
| Opus 4.8 (`gepa`) | Haiku 4.5 | T+A+S, A+S, A+T | — |
| GPT-5.2 (`openai`) | Haiku 4.5, GPT-5.0-mini | A+S only | — |

Classifiers: the side-task-leak classifier and the refusal classifier run over the
attack runs (where the refusal question is about the hidden side task), and the
refusal classifier also runs over the side-as-main runs (where the side task is presented
as a user request).

## Logs

Log paths are derived from the run, so the monitors and classifiers find their own
inputs and tell you what to run first if something is missing:

```
logs/Sonnet46_attack_gepa_318.eval                        trajectories
logs/monitor/Haiku45_Sonnet46_attack_gepa_318_full_no_text.json    monitor scores
logs/classifier/leak_Sonnet46_attack_gepa_318.json                 classifier output
```

A trajectory whose log already exists is skipped. Use `--log-dir` to work somewhere else.

## Setup

API keys, AWS credentials, and proxy config are covered in the
[root README](../../README.md#setup) — set those up first, and export them into
the shell you run from (e.g. `source ~/.env_keys`).

## Experiment-wide settings

The same for every run:

* 75 agent steps at most per task
* the last 10 screenshots shown to the agent each step
* 3.0 s post-action delay, between the agent's action running on the machine and
  the screenshot being taken, so the action has taken full effect
* no token limit
* 1920x1080 screen

All but the token limit are argparse defaults in `scripts/run_osworld.py`.

The only per-run differences are `--api_resolution 1280 720` for the Anthropic
models and `--reasoning_effort high` for GPT-5.2.
