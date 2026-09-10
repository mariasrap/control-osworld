# side_task_templates

This folder is a catalog of side tasks, how each one is paired with a main task, and how our
main tasks differ from the original OSWorld benchmark. The runnable task JSONs live in
`control_osworld/evaluation_examples/examples/`.

A **side task** is a covert, harmful objective the attack-policy agent may be secretly told
to pursue while it completes a legitimate main task. There are 81 side task templates, and
each pairing reuses one of them: first paired one-to-one with 81 carefully reviewed main
tasks, then reused across the 237 remaining main tasks (318 main tasks in total).

## Files

- `81_side_task_templates.csv` — the 81 side task templates, created with the
  `side_task_generation_pipeline`. One row per side task, with its instruction (a hand-held
  and a non-hand-held version), scoring criteria, category, and our feasibility /
  harmfulness / subtlety / clarity ratings from that pipeline.
- `81_side_task_templates_paired.csv` — the baseline: the 81 templates paired one-to-one
  with main tasks, the more carefully reviewed set of pairings.
- `237_remaining_tasks_paired.csv` — the 237 remaining main tasks, each paired with one of
  the 81 templates. Same shape as the baseline paired file, without the trajectory-divergence
  columns.
- `osworld_task_deviations.md` — the full per-task list of how our main tasks differ from
  OSWorld (summarized below).

The two paired CSVs share these columns:

- `id` — `<domain>_<osworld-task-uuid>`, the main task this side task is paired with.
- `side_task_name` — which template from the catalog.
- `main_task_instruction` / `side_task_instruction` — what the agent is asked to do.
- `Pairing-specific considerations` — any edit made for this pairing (instruction, scorer,
  or config). This column is the source of truth for the deviations below.
- `incompatibility` — a pairing we flagged as unusable, with the reason.

## How our tasks differ from OSWorld

The main tasks come from [OSWorld](https://github.com/xlang-ai/OSWorld) (Xie et al., 2024).
Most are used as-is; `osworld_task_deviations.md` has the complete per-task record. In short:

**Excluded (51 of 369).** Of the 369 OSWorld tasks in the pool, we kept 318 and dropped 51:

- **Google Drive dependent (8)** — need a live external account, so not reproducible.
- **Design-infeasible (30)** — the correct behaviour is to answer "this is impossible",
  which leaves no legitimate trajectory for a side task to hide inside.
- **GUI-infeasible (13)** — not completable through the GUI-only interaction we score.

**Modified (of the 318 kept).** Some main tasks were changed so a side task could work with
them, in three ways:

- **Instruction (54)** — text added around the user's request so the side task becomes
  clearly malicious in context. E.g. saying a file will be shared, so password-locking it
  is sabotage.
- **Scorer (17)** — some OSWorld scorers compare against a gold-standard file, so any
  change the side task makes would fail the main task even when both were done. We adjusted
  those scorers.
- **Config (43)** — changes to the starting VM state, e.g. adding a `confidential-` prefix
  to a file, or creating the folder an exfiltration side task targets.
