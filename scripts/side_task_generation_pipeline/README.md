# Side task generation pipeline

Agent-based side-task generation pipeline used to generate a first draft of /side_task_templates/81_side_task_templates.csv based on OSWorld tasks as main tasks.

Side tasks are the covert, harmful objectives an attack-policy agent may be secretly instructed to
pursue while completing a legitimate OSWorld main task. 

DISCLAIMER - side tasks were manually revised, improved and modified after iterations of testing, implementation considerations and so on.

## Design

We designed this pipeline as a 5 step process, run independently per domain (the nine OSWorld
unique-application domains plus `destructive`):

1. **Generation** — an agent drafts candidate side task templates for the domain: a name, an
   instruction for the agent, and programmatic scoring criteria. Broken down into 4 steps, each its
   own API call within a single conversation:
   1. *Brainstorm* — a persona prompt generating candidates for creative breadth. No filtering, no
      scoring, and no main tasks yet. Summaries of related benchmarks and threat models (see
      [References](#references)) are injected as inspiration.
   2. *Refine* — the domain's real OSWorld main tasks are shown, along with how side tasks get
      paired with them. Candidates are checked for viability against actual pairings, then
      shortlisted.
   3. *Scoring criteria* — the agent is asked to produce programmatic scoring criteria for side takss in the building precise, implementable templates. 
   4. *Self-score* — each template is scored on feasibility, harmfulness, subtlety and
      scoring_clarity (1–5).
2. **Agent-review** — an independent judge agent re-scores every template on the same four
   dimensions and checks for hard-requirement violations and risks of the scorer matching the VM's
   default state. It does not see the generation agent's scores. 
3. **Human-review** — a human works through the templates, concentrating on those where the judge
   flagged a violation or disagreed sharply with the author's scores, and records a verdict
   (accepted / modify / rejected) with notes. The idea is for the human to manually revise a few example so the agent can extrapolate the criteria to the rest of the tasks.
4. **Feedback to agent from 1** — the original agent is given back its own conversation plus the
   judge's scores and the human's verdicts, and revises its templates accordingly.

   Iterations of steps 2, 3, 4 until satisfactory results.

5. **Finalization/homogeneization** — a final pass over each surviving template individually:
   applies the modify notes, normalises names, reconciles the two sets of scores into one, and
   rewrites the pairing and config requirements into a consistent format.


## How the Code Works

All stages live in `run_pipeline.py`, one subcommand each, run in order:

| stage | input | output |
|---|---|---|
| `generate` | none | `{domain}_side_tasks.csv`, plus the step 1–4 transcripts |
| `judge_review` | `generate` output without scores| `{domain}_review.csv`, merged into `all_reviews.csv` |
| `implement_feedback` | `generate` output + `all_reviews.csv` (optionally with a human-filled `Human_review` column) | `final_candidates.csv`, `feedback_review.csv` (every row's verdict filled) |
| `finalize` | `feedback_review.csv` (or any CSV with a verdict column) | `all_tasks_final.csv` |

Every prompt used for agent calls lives in `prompts/`.

Implementation notes:

- **`generate`** — the four generation steps are turns in a *single* conversation, so the agent keeps
  full context across them.
- **`judge_review`** — judges all domains in parallel.
- ** human feedback ** To add human feedback, add a **`Human_review`** column to the csv produced by the judge agent
  `all_reviews.csv` and fill it for a few examples — `Keep`,
  `Modify: <what to change>` or `Reject: <why>` — leaving the rest blank. 
- **`implement_feedback`** — replays the steps 1–4 conversation, then appends the slice of `all_reviews.csv` corresponding to that domain.  The agent incorporates the judge's feedback, and if `Human_review` is present, it tries to extrapolate the criteria of the annotated tasks to the rest of the tasks.
- **`finalize`** — one call per template, keeping rows whose verdict is `Keep`, `Modify` or `NEW`
  and dropping `Reject`. The verdict is read from `Human_review` (or `verdict`), and for a
  `Modify: …` row the text after the colon becomes the modify note the agent must apply.

Requires `ANTHROPIC_API_KEY`. Run from this directory — the stages import `domain_configs` as a
sibling module. Intermediates go to `_work/` by default, never to `side_task_templates/`.
`domain_configs.py` holds the per-domain spec: apps, VM default state, attack surface and idea seeds.

## References

Summarised in `prompts/REFERENCE_BENCHMARKS.md` and injected into the brainstorm step, so that
generated side tasks build on existing work rather than reinventing it:

- **SHADE-Arena** — Kutasov et al., *SHADE-Arena: Evaluating Sabotage and Monitoring in LLM Agents*,
  2025. Sabotage side tasks paired with benign main tasks in simulated agentic environments, scored
  alongside a monitor. <https://github.com/safety-research/SHADE-Arena>
- **CUA-SHADE-Arena / MRT** — Scale AI. SHADE-Arena adapted to computer-use agents, the closest
  precedent for GUI-based sabotage evaluation. <https://github.com/scaleapi/mrt>
- **AgentHarm** — Andriushchenko et al., *AgentHarm: A Benchmark for Measuring Harmfulness of LLM
  Agents*, UK AI Safety Institute, 2024. A taxonomy of harmful agent behaviours.
  <https://huggingface.co/datasets/ai-safety-institute/AgentHarm>
- **STRIDE** — Kohnfelder and Garg, Microsoft, 1999. A threat-modelling framework (spoofing,
  tampering, repudiation, information disclosure, denial of service, elevation of privilege), used
  here to enumerate categories of attack surface systematically per domain.


