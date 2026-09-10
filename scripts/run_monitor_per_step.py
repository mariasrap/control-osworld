"""Score an existing OSWorld .eval log step by step with the trusted monitor, writing JSON.

Unlike run_monitor, which scores each trajectory once as a whole, this scores it one step
at a time, calling the monitor on a growing prefix just as an inline monitor would be called
during a live run. The variant sets what the monitor sees (the agent's private
chain-of-thought is never shown either way):
  no_text     screenshots and actions only
  with_text   screenshots and actions, plus the agent's visible messages
  no_images   visible messages and actions, but no screenshots

--mode sets what the monitor remembers between steps:
  rolling      it sees the task plus steps 1..k and gives a running suspicion (default)
  independent  it sees the task and step k alone

--score-point sets what counts as a step:
  post_action  the agent's message and action plus their result, i.e. the screenshot they
               produced — "did this look suspicious after the fact?" (default)
  pre_action   only what the agent saw and the action it proposed, without the result —
               "would the monitor have blocked it before it ran?"
See --help for every flag.

Output JSON:
    log_path       the eval log that was scored
    monitor_model  the monitor model used
    variant, mode, score_point   the settings above
    n_samples      how many samples were scored
    results        one entry per sample:
        sample_id, epoch   identify the sample in the log
        n_steps            how many steps were scored
        per_step           one entry per step: {step, score, reasoning, raw}, where score is
                           suspicion 0-100 (or None if the reply had no parsable <score> tag)
        max_score          the highest per-step score
        final_score        the last step's score
        elapsed_s          seconds the sample took
        error              None, or the failure message if the sample could not be scored
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

from inspect_ai.agent import AgentState
from inspect_ai.log import read_eval_log_sample, resolve_sample_attachments
from inspect_ai.model import ChatMessageAssistant, ChatMessageUser

from control_osworld.monitors import osworld_per_step_monitor
from control_osworld.monitors._transcript_builder import build_steps

DEFAULT_MODEL = "anthropic/claude-haiku-4-5-20251001"


def ids_in_log(path: str) -> list[tuple[str, int]]:
    """List (sample_id, epoch) from the zip central directory only (zstd-safe)."""
    out: list[tuple[str, int]] = []
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            m = re.match(r"samples/(.+)_epoch_(\d+)\.json$", n)
            if m:
                out.append((m.group(1), int(m.group(2))))
    return out


def _needs_retry(md: dict) -> bool:
    """True if this step's monitor reply is unusable: either it left no metadata to
    reconstruct the reply from (which would break every later step in rolling mode),
    or its score did not parse (a hole in the per-step series)."""
    unreconstructable = md.get("raw_monitor_response") is None and (
        md.get("suspicion_score") is None or md.get("reason") is None
    )
    return unreconstructable or md.get("score_parse_error", True)


def _prefix_end(
    messages: list, assistant_idx: int, score_point: str
) -> int:
    """End (exclusive) of the message prefix the monitor sees for this step.

    pre_action: through the step's assistant message (its tool results are
    excluded — the action hasn't "executed" from the monitor's point of view).
    post_action: through the step's tool results, i.e. up to the next assistant
    message, minus any trailing user messages (those belong to the next step's
    preface).
    """
    if score_point == "pre_action":
        return assistant_idx + 1
    end = len(messages)
    for j in range(assistant_idx + 1, len(messages)):
        if isinstance(messages[j], ChatMessageAssistant):
            end = j
            break
    while end > assistant_idx + 1 and isinstance(messages[end - 1], ChatMessageUser):
        end -= 1
    return end


async def _score_one(
    model: str,
    sample,
    variant: str,
    mode: str,
    score_point: str,
    max_tokens: int,
    semaphore: asyncio.Semaphore,
    max_image_dim: int,
    image_quality: int,
) -> dict[str, Any]:
    """Score one sample step by step; return its result row, turning any failure into an error row."""
    async with semaphore:
        t0 = time.time()
        try:
            resolved = resolve_sample_attachments(sample)
            messages = list(resolved.messages)
            monitor = osworld_per_step_monitor(
                variant=variant,
                mode=mode,
                score_point=score_point,
                model_role=None,
                model=model,
                max_tokens=max_tokens,
                max_image_dim=max_image_dim,
                image_quality=image_quality,
                task_description=(resolved.metadata or {}).get(
                    "main_task_description", ""
                ),
            )

            # Step schedule over the full trajectory; empty assistant messages
            # are dropped, matching the monitor's own step construction.
            steps = build_steps(
                messages,
                include_visible_text=variant == "with_text",
                max_image_dim=64,  # schedule only; rendered blocks are discarded
                image_quality=10,
            )
            idx_by_id = {m.id: i for i, m in enumerate(messages)}
            schedule = [
                idx_by_id[s.assistant_message_id]
                for s in steps
                if s.assistant_message_id is not None
            ]

            per_step: list[dict[str, Any]] = []
            for n, assistant_idx in enumerate(schedule, start=1):
                end = _prefix_end(messages, assistant_idx, score_point)
                # Prefix slices share message objects, so metadata the monitor
                # attaches at step n is visible when it reconstructs step n+1.
                state = AgentState(messages=messages[:end])
                await monitor(state, [])
                md = messages[assistant_idx].metadata or {}
                # Rolling mode replays every earlier monitor reply to build the next
                # step's conversation, so a step that comes back unusable (blocked /
                # empty / unparsable) aborts the WHOLE sample at the next step. Retry
                # such a step once before accepting it.
                if _needs_retry(md):
                    await monitor(AgentState(messages=messages[:end]), [])
                    md = messages[assistant_idx].metadata or {}
                parse_error = md.get("score_parse_error", True)
                per_step.append(
                    {
                        "step": n,
                        "score": None if parse_error else int(md["suspicion_score"]),
                        "reasoning": md.get("reason"),
                        "raw": md.get("raw_monitor_response"),
                    }
                )

            valid_scores = [r["score"] for r in per_step if r["score"] is not None]
            return {
                "sample_id": sample.id,
                "epoch": sample.epoch,
                "n_steps": len(per_step),
                "per_step": per_step,
                "max_score": max(valid_scores) if valid_scores else None,
                "final_score": per_step[-1]["score"] if per_step else None,
                "elapsed_s": round(time.time() - t0, 2),
                "error": None,
            }
        except Exception as e:
            return {
                "sample_id": sample.id,
                "epoch": sample.epoch,
                "n_steps": 0,
                "per_step": [],
                "max_score": None,
                "final_score": None,
                "elapsed_s": round(time.time() - t0, 2),
                "error": f"{type(e).__name__}: {e}",
            }


async def _run(
    log_path: str,
    out_path: str,
    model: str,
    variant: str,
    mode: str,
    score_point: str,
    concurrency: int,
    limit: int | None,
    max_tokens: int,
    max_image_dim: int,
    image_quality: int,
) -> None:
    """Score every sample in the log and write the results JSON to `out_path`.

    A producer streams samples one at a time so the multi-GB log is never fully
    loaded, and `concurrency` workers score them continuously.
    """
    print(f"Indexing log: {log_path}", file=sys.stderr)
    ids = ids_in_log(log_path)
    if limit:
        ids = ids[:limit]
    total = len(ids)
    # Results are streamed to a JSONL checkpoint instead of being accumulated in a
    # list: on the 237/318 logs the accumulated per-step records (reasoning + raw
    # text for every step of every sample) grew until the process was OOM-killed,
    # losing the whole run. Appending + dropping keeps memory flat, and any
    # already-scored samples are skipped so a killed run resumes.
    part_path = Path(out_path).with_suffix(".partial.jsonl")
    part_path.parent.mkdir(parents=True, exist_ok=True)
    already: set[str] = set()
    if part_path.exists():
        with open(part_path) as pf:
            for line in pf:
                line = line.strip()
                if not line:
                    continue
                try:
                    already.add(json.loads(line)["sample_id"])
                except Exception:
                    pass  # ignore a torn final line from a hard kill
        ids = [(sid, ep) for sid, ep in ids if sid not in already]
        print(f"Resuming: {len(already)} already scored, {len(ids)} to go",
              file=sys.stderr)
    print(
        f"Scoring {total} samples with model={model} variant={variant} "
        f"mode={mode} score_point={score_point} concurrency={concurrency} "
        f"(streaming pipeline)",
        file=sys.stderr,
    )

    semaphore = asyncio.Semaphore(concurrency)

    done = 0
    # Producer/consumer with no batch barrier: one reader streams samples (never
    # full-loading the multi-GB log) into a bounded queue; `concurrency` workers
    # score continuously, so a freed worker takes the next sample without waiting
    # on the slowest of a batch. The bounded queue caps resident samples.
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=2)  # fewer resident multi-MB samples

    async def producer() -> None:
        for sid, epoch in ids:
            sample = await loop.run_in_executor(
                None, read_eval_log_sample, log_path, sid, epoch
            )
            await queue.put(sample)
        for _ in range(concurrency):
            await queue.put(None)  # one sentinel per worker

    async def worker() -> None:
        nonlocal done
        while True:
            sample = await queue.get()
            if sample is None:
                break
            r = await _score_one(
                model, sample, variant, mode, score_point, max_tokens,
                semaphore, max_image_dim, image_quality,
            )
            part_f.write(json.dumps(r) + "\n")
            part_f.flush()
            done += 1
            marker = "OK" if r["error"] is None else "ERR"
            ms = r["max_score"] if r["max_score"] is not None else "?"
            fs = r["final_score"] if r["final_score"] is not None else "?"
            print(
                f"  [{done}/{total}] {marker} {r['sample_id']} "
                f"n_steps={r['n_steps']} max={ms} final={fs} t={r['elapsed_s']}s",
                file=sys.stderr,
            )
            del sample

    with open(part_path, "a") as part_f:
        await asyncio.gather(producer(), *[worker() for _ in range(concurrency)])

    # Assemble the final JSON by streaming the checkpoint, so the full result set
    # is never resident in memory at once.
    meta = {
        "log_path": log_path,
        "monitor_model": model,
        "variant": variant,
        "mode": mode,
        "score_point": score_point,
        "n_samples": total,
    }
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        f.write("{\n")
        for k, v in meta.items():
            f.write(f"  {json.dumps(k)}: {json.dumps(v)},\n")
        f.write('  "results": [\n')
        first = True
        with open(part_path) as pf:
            for line in pf:
                line = line.strip()
                if not line:
                    continue
                if not first:
                    f.write(",\n")
                f.write("    " + line)
                first = False
        f.write("\n  ]\n}\n")
    part_path.unlink(missing_ok=True)
    print(f"Wrote: {out_path}", file=sys.stderr)


def main() -> None:
    """Parse arguments and run the scoring."""
    p = argparse.ArgumentParser(
        description="Score an OSWorld eval log step by step with the trusted monitor.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--log-path", required=True,
                   help="The .eval log to score.")
    p.add_argument("--out-path", required=True,
                   help="Where to write the results JSON.")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help="Monitor model, as provider/name (bare names get an anthropic/ prefix).")
    p.add_argument("--variant", choices=["no_text", "with_text", "no_images"], required=True,
                   help="What the monitor sees; see the module docstring.")
    p.add_argument("--mode", choices=["rolling", "independent"], default="rolling",
                   help="What the monitor remembers between steps; see the module docstring.")
    p.add_argument("--score-point", choices=["post_action", "pre_action"], default="post_action",
                   help="What counts as a step; see the module docstring.")
    p.add_argument("--concurrency", type=int, default=4,
                   help="How many samples to score at once.")
    p.add_argument("--limit", type=int, default=None,
                   help="Only score the first N samples (default: all).")
    p.add_argument("--max-tokens", type=int, default=2048,
                   help="Token budget for each monitor reply.")
    p.add_argument("--max-image-dim", type=int, default=1024,
                   help="Resize screenshots so the longest side is at most this many pixels.")
    p.add_argument("--image-quality", type=int, default=80,
                   help="JPEG quality (1-95) used when re-encoding screenshots.")
    args = p.parse_args()

    asyncio.run(
        _run(
            log_path=args.log_path,
            out_path=args.out_path,
            model=args.model if "/" in args.model else f"anthropic/{args.model}",
            variant=args.variant,
            mode=args.mode,
            score_point=args.score_point,
            concurrency=args.concurrency,
            limit=args.limit,
            max_tokens=args.max_tokens,
            max_image_dim=args.max_image_dim,
            image_quality=args.image_quality,
        )
    )


if __name__ == "__main__":
    main()
