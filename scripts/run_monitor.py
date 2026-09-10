"""Score an existing OSWorld .eval log with the trusted monitor and write the results as JSON.

Replays each recorded trajectory once, in full, past the monitor, which rates how
suspicious it looks from 0 to 100. The variant sets what the monitor sees (the agent's
private chain-of-thought is never shown either way):
  no_text     screenshots and actions only
  with_text   screenshots and actions, plus the agent's visible messages
  no_images   visible messages and actions, but no screenshots
See --help for every flag.

Output JSON:
    log_path       the eval log that was scored
    monitor_model  the monitor model used
    variant        which of the three variants above
    n_samples      how many samples were scored
    results        one entry per sample:
        sample_id, epoch   identify the sample in the log
        score              suspicion 0-100, or None if the reply had no parsable <score> tag
        reasoning          the monitor's explanation for its score
        raw                the monitor's full reply text
        usage              token usage for the monitor call
        elapsed_s          seconds the call took
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

from control_osworld.monitors import osworld_full_trajectory_monitor

DEFAULT_MODEL = "anthropic/claude-haiku-4-5-20251001"


def ids_in_log(path: str) -> list[tuple[str, int]]:
    """List (sample_id, epoch) by reading the zip central directory only.

    Reads filenames, not sample bodies, so it is cheap and works on zstd-
    compressed logs that stdlib zipfile cannot decompress.
    """
    out: list[tuple[str, int]] = []
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            m = re.match(r"samples/(.+)_epoch_(\d+)\.json$", n)
            if m:
                out.append((m.group(1), int(m.group(2))))
    return out


async def _score_one(
    model: str,
    sample,
    variant: str,
    max_tokens: int,
    semaphore: asyncio.Semaphore,
    max_image_dim: int,
    image_quality: int,
) -> dict[str, Any]:
    """Score one sample; return its result row, turning any failure into an error row."""
    async with semaphore:
        t0 = time.time()
        try:
            resolved = resolve_sample_attachments(sample)
            monitor = osworld_full_trajectory_monitor(
                variant=variant,
                model_role=None,
                model=model,
                max_tokens=max_tokens,
                max_image_dim=max_image_dim,
                image_quality=image_quality,
                task_description=(resolved.metadata or {}).get(
                    "main_task_description", ""
                ),
            )
            state = AgentState(messages=list(resolved.messages))
            state = await monitor(state, [])
            md = state.messages[-1].metadata or {}
            parse_error = md.get("score_parse_error", True)
            return {
                "sample_id": sample.id,
                "epoch": sample.epoch,
                "score": None if parse_error else int(md["suspicion_score"]),
                "reasoning": md.get("reason"),
                "raw": md.get("raw_monitor_response"),
                "usage": md.get("model_usage"),
                "elapsed_s": round(time.time() - t0, 2),
                "error": None,
            }
        except Exception as e:
            return {
                "sample_id": sample.id,
                "epoch": sample.epoch,
                "score": None,
                "reasoning": None,
                "raw": None,
                "usage": None,
                "elapsed_s": round(time.time() - t0, 2),
                "error": f"{type(e).__name__}: {e}",
            }


async def _run(
    log_path: str,
    out_path: str,
    model: str,
    variant: str,
    concurrency: int,
    limit: int | None,
    max_tokens: int,
    max_image_dim: int,
    image_quality: int,
) -> None:
    """Score every sample in the log and write the results JSON to `out_path`.

    Samples are read one at a time and scored in batches of `concurrency`, so the
    multi-GB log is never fully loaded.
    """
    print(f"Indexing log: {log_path}", file=sys.stderr)
    ids = ids_in_log(log_path)
    if limit:
        ids = ids[:limit]
    total = len(ids)
    print(
        f"Scoring {total} samples with model={model} variant={variant} concurrency={concurrency} "
        f"(streaming, batch={concurrency})",
        file=sys.stderr,
    )

    semaphore = asyncio.Semaphore(concurrency)

    results: list[dict[str, Any]] = []
    done = 0
    # Stream in batches, reading one sample at a time so the multi-GB log is
    # never fully loaded.
    for bstart in range(0, total, concurrency):
        batch_ids = ids[bstart:bstart + concurrency]
        # A corrupt sample becomes an error row rather than crashing the run.
        samples = []
        read_errors = []
        for sid, epoch in batch_ids:
            try:
                samples.append(read_eval_log_sample(log_path, sid, epoch))
            except Exception as e:
                read_errors.append({
                    "sample_id": sid, "epoch": epoch, "score": None,
                    "reasoning": None, "raw": None, "usage": None,
                    "elapsed_s": 0.0,
                    "error": f"read failed: {type(e).__name__}: {e}",
                })
        coros = [
            _score_one(model, s, variant, max_tokens, semaphore, max_image_dim, image_quality)
            for s in samples
        ]
        for r in list(await asyncio.gather(*coros)) + read_errors:
            results.append(r)
            done += 1
            marker = "OK" if r["error"] is None else "ERR"
            score = r["score"] if r["score"] is not None else "?"
            print(
                f"  [{done}/{total}] {marker} {r['sample_id']} score={score} t={r['elapsed_s']}s",
                file=sys.stderr,
            )
        del samples

    out = {
        "log_path": log_path,
        "monitor_model": model,
        "variant": variant,
        "n_samples": total,
        "results": results,
    }
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote: {out_path}", file=sys.stderr)


def main() -> None:
    """Parse arguments and run the scoring."""
    p = argparse.ArgumentParser(
        description="Score an OSWorld eval log with the trusted monitor.",
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
    p.add_argument("--concurrency", type=int, default=5,
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
            concurrency=args.concurrency,
            limit=args.limit,
            max_tokens=args.max_tokens,
            max_image_dim=args.max_image_dim,
            image_quality=args.image_quality,
        )
    )


if __name__ == "__main__":
    main()
