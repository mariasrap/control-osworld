"""GEPA attack-prompt optimization: the whole loop in one module.

The run_osworld and monitor subprocesses inherit this environment, so ensure API keys are in the environement before launching (Anthropic for agent, monitor and reflection; AWS for the
sandbox). Every rollout is a real VM run, so mind MAX_METRIC_CALLS.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import pickle
import shutil
import subprocess
import time
from pathlib import Path

import gepa

from scripts.attack_prompt_gepa import config as C
from scripts.attack_prompt_gepa.helpers import (
    FAIL_SCORE, candidate_hash, extract_block, looks_like_refusal, read_eval_log,
    read_monitor_json, render_examples, split_domain, task_score,
)


def prepare_config_files(candidate: dict, task_ids: list[str]) -> tuple[Path, Path | None, Path]:
    """Write the prompt, reminder and task list this run needs."""
    C.WORK_DIR.mkdir(parents=True, exist_ok=True)
    tag = candidate_hash(candidate)

    prompt_file = C.WORK_DIR / f"cand_{tag}_prompt.txt"
    prompt_file.write_text(candidate["attack_prompt"])

    # An empty reminder means "inject no reminder", not "inject a blank one": GEPA can
    # start from no reminder and have the reflection agent write one in a later round.
    reminder_file = None
    if (candidate.get("attack_reminder") or "").strip():
        reminder_file = C.WORK_DIR / f"cand_{tag}_reminder.txt"
        reminder_file.write_text(candidate["attack_reminder"])

    # A task can repeat within a GEPA batch and run_osworld rejects duplicates, so list
    # each one once; its cache entry covers the repeats.
    tasks: dict[str, list[str]] = {}
    for sid in dict.fromkeys(task_ids):
        domain, uuid = split_domain(sid)
        tasks.setdefault(domain, []).append(uuid)
    test_config = C.WORK_DIR / f"cand_{tag}_tasks.json"
    test_config.write_text(json.dumps(tasks, indent=2))

    return prompt_file, reminder_file, test_config


def run_trajectories(prompt_file: Path, reminder_file: Path | None, test_config: Path,
                     log_out: Path) -> Path:
    """Run the agent on real VMs and return the eval log it produced."""
    cmd = [
        str(C.REPO / ".venv/bin/python"), str(C.RUN_OSWORLD),
        "--attack_prompt_file", str(prompt_file),
        "--test_config", str(test_config),
        "--mode", C.MODE,
        "--model", C.ATTACKER_MODEL,
        "--max_steps", str(C.MAX_STEPS),
        "--max_sandboxes", str(C.MAX_SANDBOXES),
        "--token_limit", C.TOKEN_LIMIT,
        "--display", "plain",
    ]
    if C.PER_STEP_SIDE_EVAL:
        cmd.append("--per_step_side_eval")
    if C.ATTACK_REMINDERS and reminder_file is not None:
        cmd += ["--attack_reminders",
                "--attack_reminder_every_n_steps", str(C.ATTACK_REMINDER_EVERY_N),
                "--attack_reminder_file", str(reminder_file)]

    start = time.time()
    proc = subprocess.run(cmd, cwd=str(C.REPO), env=os.environ.copy(),
                          capture_output=True, text=True)
    log_out.write_text(proc.stdout + "\n----STDERR----\n" + proc.stderr)
    if proc.returncode != 0:
        raise RuntimeError(f"run_osworld failed (rc={proc.returncode}); see {log_out}")

    # Find the log by modification time: inspect names it, and with --display plain the
    # printed path is line-wrapped, so a regex on stdout misses it. Only one candidate
    # runs at a time, so the newest log touched during this run is unambiguous.
    produced = [p for p in C.LOGS.glob("*.eval") if p.stat().st_mtime >= start - 2]
    if not produced:
        raise RuntimeError(f"no .eval log produced by run_osworld; see {log_out}")
    return max(produced, key=lambda p: p.stat().st_mtime).resolve()


def run_monitors(eval_log: Path, tag: str) -> tuple[dict, dict]:
    """Score a log twice, once with the agent's visible text and once without it."""
    out = []
    for variant in ("with_text", "no_text"):
        out_path = C.WORK_DIR / f"cand_{tag}_{variant}.json"
        proc = subprocess.run(
            [str(C.REPO / ".venv/bin/python"), str(C.REPO / "scripts/run_monitor.py"),
             "--log-path", str(eval_log), "--out-path", str(out_path),
             "--model", C.TARGET_MONITOR_MODEL, "--variant", variant],
            cwd=str(C.REPO), env=os.environ.copy(), capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"run_monitor ({variant}) failed: {proc.stderr[-2000:]}")
        out.append(read_monitor_json([out_path]))
    return out[0], out[1]


def evaluate_candidate(candidate: dict, task_ids: list[str]) -> dict[str, dict]:
    """Run one attack prompt on some tasks and score how suspicious each run looked."""
    tag = candidate_hash(candidate)
    prompt_file, reminder_file, test_config = prepare_config_files(candidate, task_ids)
    eval_log = run_trajectories(prompt_file, reminder_file, test_config,
                                C.WORK_DIR / f"cand_{tag}_run.out")
    with_text, no_text = run_monitors(eval_log, tag)
    samples = read_eval_log(eval_log, set(task_ids))

    records = {}
    for sid in task_ids:
        if sid not in with_text or sid not in no_text:
            continue   # the task errored; GEPA tolerates missing ones
        w, n = with_text[sid]["score"], no_text[sid]["score"]
        info = samples.get(sid, {})
        loss, score = task_score(w, n, info.get("side_success", False))
        records[sid] = {
            "sample_id": sid, "with_text": w, "no_text": n, "gap": w - n,
            "side_success": info.get("side_success", False), "loss": loss, "score": score,
            "wt_reasoning": with_text[sid]["reasoning"], "nt_reasoning": no_text[sid]["reasoning"],
            "main_task": info.get("main_task", ""), "side_task": info.get("side_task", ""),
            "trajectory": info.get("trajectory", ""), "eval_log": str(eval_log),
        }
    return records


class ReflectionRefused(RuntimeError):
    """The reflection agent declined to rewrite the prompt."""

    def __init__(self, reply: str) -> None:
        super().__init__("reflection agent refused at every effort level")
        self.reply = reply


def assemble_reflection_prompt(meta_body: str, candidate: dict, records: list[dict]) -> str:
    """Fill the meta-prompt with the current prompt, its reminder and the example runs."""
    reminder = (candidate.get("attack_reminder") or "").strip()
    return (meta_body
            .replace("%%CURRENT_PROMPT%%", candidate.get("attack_prompt", ""))
            .replace("%%CURRENT_REMINDER%%", reminder or "(empty — there is no reminder yet)")
            .replace("%%SIDE_INFO%%", render_examples(records)))


def run_reflection_llm(prompt: str, model: str) -> dict[str, str]:
    """Ask for a rewritten prompt and reminder, dropping effort if the model refuses.

    A refusal happens before any rollout, so a retry costs one call and nothing on the
    VMs. Raises ReflectionRefused if every effort level declines.
    """
    import anthropic
    for effort in [C.REFLECTION_EFFORT, *C.REFLECTION_EFFORT_FALLBACKS]:
        # Native SDK, not litellm, whose installed version rejects effort="xhigh"/"max"
        # for opus-4-8. The model id may arrive with a litellm provider prefix.
        reply = anthropic.Anthropic().messages.create(
            model=model.split("/")[-1],
            max_tokens=C.REFLECTION_MAX_TOKENS,
            thinking=C.REFLECTION_THINKING,
            output_config={"effort": effort},
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in reply.content if getattr(b, "type", None) == "text")
        new_prompt, new_reminder = extract_block("prompt", text), extract_block("reminder", text)
        if not looks_like_refusal(text) and new_prompt and new_reminder:
            return {"attack_prompt": new_prompt, "attack_reminder": new_reminder}
    raise ReflectionRefused(text)


class AttackAdapter(gepa.GEPAAdapter):
    """Connects GEPA to our setup: it asks for scores and rewrites, we supply them."""

    def __init__(self, cache: dict, meta_body: str, stop_on_refusal: "StopOnRefusal",
                 stop_on_plateau: "StopOnPlateau"):
        self.cache = cache
        self.meta_body = meta_body
        self.stop_on_refusal = stop_on_refusal
        self.stop_on_plateau = stop_on_plateau

    def evaluate(self, batch, candidate, capture_traces: bool = False):
        """Score these tasks with this prompt, running only what is not cached."""
        ch = candidate_hash(candidate)
        missing = [b["sample_id"] for b in batch if (ch, b["sample_id"]) not in self.cache]
        if missing:
            for sid, rec in evaluate_candidate(candidate, missing).items():
                self.cache[(ch, sid)] = rec

        if {b["sample_id"] for b in batch} == self.stop_on_plateau.task_ids:
            self.stop_on_plateau.record(self.cache, ch)

        records = [self.cache.get((ch, b["sample_id"])) or _failed_record(b["sample_id"])
                   for b in batch]
        return gepa.EvaluationBatch(
            outputs=records, scores=[r["score"] for r in records],
            trajectories=records if capture_traces else None, objective_scores=None,
        )

    def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
        """Hand GEPA the run records; we format them ourselves at proposal time."""
        source = eval_batch.trajectories if eval_batch.trajectories is not None else eval_batch.outputs
        return {comp: list(source) for comp in components_to_update}

    def propose_new_texts(self, candidate, reflective_dataset, components_to_update):
        """Rewrite prompt and reminder together, on the candidate GEPA selected.

        Defining this takes the proposal step over from GEPA, which would otherwise call
        the reflection agent itself with its own template, one component at a time.
        """
        prompt = assemble_reflection_prompt(
            self.meta_body, candidate, self._examples(candidate, reflective_dataset))
        try:
            new = run_reflection_llm(prompt, C.REFLECTION_LM)
        except ReflectionRefused as e:
            # GEPA swallows exceptions here and keeps going, so flag the stopper too —
            # otherwise the refusal text would be stored as the next prompt.
            self.stop_on_refusal.reply = e.reply
            raise
        return {c: new[c] for c in components_to_update if c in new}

    def _examples(self, candidate, reflective_dataset) -> list[dict]:
        """Show the reflection agent the most suspicious runs plus two clean ones.

        These come from the parent's cache rather than GEPA's screening minibatch, so
        showing more examples costs no rollouts.
        """
        ch = candidate_hash(candidate)
        scored = sorted((r for (h, _), r in self.cache.items()
                         if h == ch and r.get("with_text") is not None),
                        key=lambda r: r["with_text"], reverse=True)
        if not scored:
            return next(iter(reflective_dataset.values()), [])
        n = C.N_REFLECTION_EXAMPLES
        return scored[:n - 2] + scored[-2:] if len(scored) > n else scored


def _failed_record(sample_id: str) -> dict:
    """Placeholder for a task that errored, scored so GEPA never prefers it."""
    return {"sample_id": sample_id, "score": FAIL_SCORE, "loss": 2 * C.MONITOR_SCALE,
            "with_text": None, "no_text": None, "gap": None, "side_success": False,
            "wt_reasoning": "", "nt_reasoning": "", "main_task": "", "side_task": "",
            "trajectory": ""}


class StopOnRefusal:
    """Stops the run once the reflection agent has refused."""

    def __init__(self) -> None:
        self.reply: str | None = None

    def __call__(self, _state) -> bool:
        return self.reply is not None


class StopOnPlateau:
    """Stops when the best average suspicion stops improving.

    Tracks raw suspicion rather than the GEPA score, which the side-task penalty
    dominates. Seeds set the starting best but do not count toward the patience window.
    """

    def __init__(self, task_ids: list[str], min_delta: float, patience: int) -> None:
        self.task_ids = set(task_ids)
        self.min_delta = min_delta
        self.patience = patience
        self.best_history: list[float] = [float("inf")]
        self.seen: set[str] = set()

    def record(self, cache: dict, ch: str) -> None:
        """Note one full evaluation of a candidate, ignoring repeats."""
        if ch in self.seen:
            return
        scores = [cache[(ch, sid)]["with_text"] for sid in self.task_ids
                  if cache.get((ch, sid), {}).get("with_text") is not None]
        if not scores:
            return
        self.seen.add(ch)
        mean = sum(scores) / len(scores)
        self.best_history.append(min(mean, self.best_history[-1]))
        print(f"[early-stop] evaluation #{len(self.best_history) - 1}: "
              f"mean suspicion {mean:.1f} (best {self.best_history[-1]:.1f})")

    def add_seeds(self, cache: dict, seed_hashes: list[str]) -> None:
        """Set the starting best from the seeds, without counting them as evaluations."""
        for ch in seed_hashes:
            self.record(cache, ch)
        self.best_history = [self.best_history[-1]]

    def __call__(self, _state) -> bool:
        if len(self.best_history) <= self.patience:
            return False
        return self.best_history[-self.patience - 1] - self.best_history[-1] < self.min_delta


def stop_on_budget(max_calls: int, minibatch: int, n_tasks: int):
    """Stops before a round that could not finish inside the budget.

    GEPA checks the budget only at the top of a round, so a round that starts under it
    still runs to completion and overshoots. We reserve a whole round instead.
    """
    headroom = 2 * minibatch + n_tasks
    return lambda state: state.total_num_evals + headroom > max_calls


def prime_seed(seed: C.SeedRun, task_ids: list[str]) -> tuple[dict, float]:
    """Score a past run's tasks into cache entries, without spending any rollout."""
    for path in [seed.prompt_path, *seed.eval_log, *seed.with_text_json, *seed.no_text_json]:
        if not path.exists():
            raise SystemExit(f"seed {seed.name}: missing {path}")
    for variant, paths in (("with_text", seed.with_text_json), ("no_text", seed.no_text_json)):
        for p in paths:
            meta = json.load(open(p))
            # Some monitor runs stored a bare model id, others add a
            # provider prefix. Same model, so compare the last segment.
            if meta.get("monitor_model", "").split("/")[-1] != C.TARGET_MONITOR_MODEL.split("/")[-1]:
                raise SystemExit(f"seed {seed.name}: {p.name} used {meta.get('monitor_model')}")
            if meta.get("variant") != variant:
                raise SystemExit(f"seed {seed.name}: {p.name} is not the {variant} variant")

    ch = candidate_hash(seed_to_candidate(seed))
    with_text = read_monitor_json(seed.with_text_json)
    no_text = read_monitor_json(seed.no_text_json)
    samples: dict[str, dict] = {}
    for log in seed.eval_log:
        samples.update(read_eval_log(log, set(task_ids)))

    cache, total = {}, 0.0
    for sid in task_ids:
        if sid not in with_text or sid not in no_text:
            continue
        w, n = with_text[sid]["score"], no_text[sid]["score"]
        info = samples.get(sid, {})
        loss, score = task_score(w, n, info.get("side_success", False))
        cache[(ch, sid)] = {
            "sample_id": sid, "with_text": w, "no_text": n, "gap": w - n,
            "side_success": info.get("side_success", False), "loss": loss, "score": score,
            "wt_reasoning": with_text[sid]["reasoning"], "nt_reasoning": no_text[sid]["reasoning"],
            "main_task": info.get("main_task", ""), "side_task": info.get("side_task", ""),
            "trajectory": info.get("trajectory", ""), "eval_log": str(seed.eval_log[0]),
        }
        total += score
    return cache, (total / len(cache) if cache else FAIL_SCORE)


def seed_to_candidate(seed: C.SeedRun) -> dict:
    """The candidate a seed run evaluated: its prompt and its own reminder."""
    return {"attack_prompt": seed.prompt_path.read_text(),
            "attack_reminder": seed.reminder_text}


PRIMED_CACHE_PATH = C.WORK_DIR / "primed_cache.pkl"


def load_seeds(task_ids: list[str]) -> tuple[dict, list, int]:
    """Score every seed run, reusing the saved result unless the inputs changed.

    Rebuilding means re-reading multi-GB eval logs, which is the slowest step of a
    launch, so the result is pickled against a fingerprint of the seeds and task set.
    """
    signature = hashlib.sha1(repr(task_ids).encode())
    for seed in C.SEED_RUNS:
        signature.update(candidate_hash(seed_to_candidate(seed)).encode())
        for f in [seed.prompt_path, *seed.with_text_json, *seed.no_text_json, *seed.eval_log]:
            mtime = f.stat().st_mtime_ns if f.exists() else "MISSING"
            signature.update(f"{f}:{mtime}".encode())
    sig = signature.hexdigest()[:16]

    if PRIMED_CACHE_PATH.exists():
        blob = pickle.load(open(PRIMED_CACHE_PATH, "rb"))
        if blob["sig"] == sig:
            print(f"reusing primed cache: {len(blob['primed'])} seeds")
            return blob["cache"], blob["primed"], blob["best_idx"]
        print("primed cache is stale (seeds or tasks changed) — rebuilding")

    cache: dict = {}
    primed: list[tuple[C.SeedRun, dict, dict]] = []
    best_idx, best_agg = -1, float("-inf")
    for seed in C.SEED_RUNS:
        if not seed.config_verified:
            print(f"  skipping {seed.name}: config not verified")
            continue
        entries, agg = prime_seed(seed, task_ids)
        cache.update(entries)
        primed.append((seed, seed_to_candidate(seed), entries))
        print(f"  primed {seed.name}: {len(entries)} tasks, score {agg:.1f}")
        if agg > best_agg:
            best_idx, best_agg = len(primed) - 1, agg
        gc.collect()   # release the log we just read; the box is memory-tight
    if not primed:
        raise SystemExit("No verified seed runs configured; fill C.SEED_RUNS first.")

    C.WORK_DIR.mkdir(parents=True, exist_ok=True)
    with open(PRIMED_CACHE_PATH, "wb") as fh:
        pickle.dump({"sig": sig, "cache": cache, "primed": primed, "best_idx": best_idx}, fh)
    return cache, primed, best_idx


def assemble_multiple_seeds(run_dir: Path, primed: list, best_idx: int,
                            task_ids: list[str]) -> None:
    """Start the candidate pool with every seed instead of one.

    gepa.optimize() takes a single seed_candidate, so we pre-write the state file it
    resumes from. All scores come from the seed runs, so this costs no rollouts.
    """
    from gepa.core.state import GEPAState, ValsetEvaluation

    # gepa wraps the task list in a loader whose ids are INDICES, so entries must be
    # keyed by position in `task_ids`, not by sample_id.
    position = {sid: i for i, sid in enumerate(task_ids)}

    def evaluation(entries: dict) -> ValsetEvaluation:
        return ValsetEvaluation(
            outputs_by_val_id={position[sid]: rec for (_h, sid), rec in entries.items()},
            scores_by_val_id={position[sid]: rec["score"] for (_h, sid), rec in entries.items()},
            objective_scores_by_val_id=None,
        )

    _seed, best_candidate, best_entries = primed[best_idx]
    state = GEPAState(best_candidate, evaluation(best_entries), track_best_outputs=True,
                      frontier_type=C.FRONTIER_TYPE, evaluation_cache=None)
    state.num_full_ds_evals = 1
    state.total_num_evals = len(best_entries)
    for i, (_s, candidate, entries) in enumerate(primed):
        if i == best_idx:
            continue
        state.update_state_with_new_program(
            parent_program_idx=[], new_program=candidate, valset_evaluation=evaluation(entries),
            run_dir=str(run_dir), num_metric_calls_by_discovery_of_new_program=0)
        state.num_full_ds_evals += 1
    state.save(str(run_dir))


def build_meta_prompt() -> str:
    """Read the meta-prompt and fill in the parts that never change between rounds."""
    from datetime import datetime

    from control_osworld.dataset import _SETTING_DESCRIPTION_TEMPLATE
    if not C.REFLECTION_META_PATH.exists():
        raise SystemExit(f"missing meta-prompt: {C.REFLECTION_META_PATH}")
    setting = _SETTING_DESCRIPTION_TEMPLATE.format(
        post_action_delay=3.0, current_date=datetime.today().strftime("%A, %B %d, %Y"))
    return (C.REFLECTION_META_PATH.read_text()
            .replace("%%SETTING%%", setting)
            .replace("%%HONEST_LOW%%", C.HONEST_LOW_PATH.read_text())
            .replace("%%HONEST_HIGH%%", C.HONEST_HIGH_PATH.read_text()))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", required=True, type=Path,
                    help="task-set JSON; its keys are the tasks to optimize over "
                         "(e.g. training_subset.json)")
    ap.add_argument("--resume", action="store_true",
                    help="continue the existing run instead of starting fresh")
    a = ap.parse_args()

    # 1. Load the tasks. Everything else is set in config.py.
    task_ids = list(json.load(open(a.tasks)).keys())
    instances = [{"sample_id": s} for s in task_ids]
    print(f"tasks: {len(task_ids)}")

    # 2. Prepare the multi-seed pool.
    cache, primed, best_idx = load_seeds(task_ids)
    if not a.resume and C.RUNS_DIR.exists():
        shutil.rmtree(C.RUNS_DIR)
    C.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    if not a.resume and len(primed) > 1:
        assemble_multiple_seeds(C.RUNS_DIR, primed, best_idx, task_ids)
        print(f"pool starts with {len(primed)} seeds")

    # 3. Run GEPA.
    refusal = StopOnRefusal()
    plateau = StopOnPlateau(task_ids, C.EARLY_STOP_MIN_DELTA, C.EARLY_STOP_PATIENCE)
    plateau.add_seeds(cache, [candidate_hash(c) for (_s, c, _e) in primed])
    result = gepa.optimize(
        seed_candidate=primed[best_idx][1],
        trainset=instances,
        adapter=AttackAdapter(cache, build_meta_prompt(), refusal, plateau),
        reflection_lm=None,   # our adapter proposes; see propose_new_texts
        stop_callbacks=[refusal, plateau,
                        stop_on_budget(C.MAX_METRIC_CALLS, C.REFLECTION_MINIBATCH_SIZE, len(task_ids))],
        candidate_selection_strategy=C.CANDIDATE_SELECTION,
        frontier_type=C.FRONTIER_TYPE,
        module_selector=C.MODULE_SELECTOR,
        reflection_minibatch_size=C.REFLECTION_MINIBATCH_SIZE,
        max_metric_calls=C.MAX_METRIC_CALLS,
        run_dir=str(C.RUNS_DIR),
        track_best_outputs=True,
        display_progress_bar=True,
    )

    # 4. Save the results.
    if refusal.reply is not None:
        print("\n!! the reflection agent refused; fix the meta-prompt framing and rerun."
              f"\nIts reply began:\n{refusal.reply[:800]}")
    (C.RUNS_DIR / "best_prompt.txt").write_text(result.best_candidate["attack_prompt"])
    reminder = (result.best_candidate.get("attack_reminder") or "").strip()
    if reminder:
        (C.RUNS_DIR / "best_reminder.txt").write_text(reminder)
    print(f"\nbest prompt and reminder written to {C.RUNS_DIR}")


if __name__ == "__main__":
    main()
