#!/usr/bin/env python3
"""
Agent-based side task generation pipeline — all stages behind one entry point.

Used to generate a first draft of side_task_templates/81_side_task_templates.csv, based on
OSWorld tasks as main tasks. This file is a cleaned-up reconstruction of the scripts actually
used: the prompts and the logic are faithful, the CLI is not. See README.md for the design.

The design is a 5 step process, run independently per domain, of which 4 are subcommands here
(step 3, human-review, is the manual pass over all_reviews.csv):

    stage                 input                                  output
    ------------------------------------------------------------------------------------
    generate              none                                   {domain}_side_tasks.csv
                                                                 step1..4 transcripts
    judge_review          generate output without scores         {domain}_review.csv
                                                                 all_reviews.csv
    implement_feedback    generate output + all_reviews.csv       final_candidates.csv
                          (optionally human-annotated)           feedback_review.csv
    finalize              feedback_review.csv (verdict column)      all_tasks_final.csv

`judge_review` and `implement_feedback` can be iterated until the results are satisfactory. To
add human feedback, add a `Human_review` column to all_reviews.csv and fill it for a few
examples (Keep / Modify: ... / Reject: ...), leaving the rest blank; the agent extrapolates
those criteria to the remaining tasks.

Usage — every stage takes a stage name and a working directory:

    python run_pipeline.py <stage> --out WORKDIR

    generate            writes WORKDIR/{domain}/step1..4 + {domain}_side_tasks.csv
    judge_review        writes WORKDIR/{domain}/{domain}_review.csv + WORKDIR/all_reviews.csv
    implement_feedback  writes WORKDIR/{domain}/final_candidates.csv
    finalize            writes WORKDIR/all_tasks_final.csv

Optionally, restrict any stage to a subset of domains:

    python run_pipeline.py generate --out WORKDIR --domains chrome,gimp

Run from this directory (the stages import `domain_configs` as a sibling module).
Requires ANTHROPIC_API_KEY.
"""

import argparse
import asyncio
import csv
import io
import json
import re
from pathlib import Path

import anthropic

from domain_configs import DOMAINS

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
PROMPT_DIR = SCRIPT_DIR / "prompts"
EXAMPLES_PATH = REPO_ROOT / "control_osworld/evaluation_examples/examples"

# Scratch directory for pipeline intermediates. Deliberately NOT
# `side_task_templates/` — that holds the finalized, hand-edited CSVs.
DEFAULT_WORKDIR = SCRIPT_DIR / "_work"

DEFAULT_MODEL = "claude-opus-4-6"

GEN_STEP_PROMPTS = {
    1: PROMPT_DIR / "GEN_STEP1_BRAINSTORM.md",
    2: PROMPT_DIR / "GEN_STEP2_REFINE.md",
    3: PROMPT_DIR / "GEN_STEP3_SCORING_CRITERIA.md",
    4: PROMPT_DIR / "GEN_STEP4_SELF_SCORE.md",
}
REFERENCE_PATH = PROMPT_DIR / "REFERENCE_BENCHMARKS.md"
JUDGE_PROMPT_PATH = PROMPT_DIR / "JUDGE_REVIEW.md"
FEEDBACK_PATH = PROMPT_DIR / "IMPLEMENT_FEEDBACK.md"
FEEDBACK_DESTRUCTIVE_PATH = PROMPT_DIR / "IMPLEMENT_FEEDBACK_DESTRUCTIVE.md"
FINALIZE_PATH = PROMPT_DIR / "FINALIZE.md"

# Generation steps 1-4, then the implement_feedback turn (key 5).
MAX_TOKENS = {1: 16384, 2: 16384, 3: 32768, 4: 32768, 5: 65536}

# judge_review: the judge sees only these columns — scores and notes are stripped
# so it reviews independently.
JUDGE_INPUT_COLUMNS = ["name", "category", "instruction", "scoring_criteria"]

# judge_review: dimensions scored by both the author and the judge.
SHARED_DIMENSIONS = ["feasibility", "harmfulness", "subtlety"]
LARGE_DISAGREEMENT = 2

# finalize: columns of the finalized CSV.
FINAL_COLUMNS = [
    "domain", "category", "name", "instruction", "scoring_criteria", "postconfig",
    "feasibility", "harmfulness", "subtlety", "scoring_clarity", "score_reasoning",
    "attention",
]


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------

def load_main_task_instructions(domain: str) -> str:
    """Format every OSWorld main task instruction in a domain as a list."""
    domain_path = EXAMPLES_PATH / domain
    if not domain_path.exists():
        # NB: the `destructive` domain has no examples/ directory of its own —
        # it draws on multi_apps. Historically this returned the placeholder.
        return "(No main tasks found)"

    tasks = []
    for f in sorted(domain_path.iterdir()):
        if f.suffix != ".json" or f.is_dir():
            continue
        data = json.loads(f.read_text())
        tasks.append(f"- [{data.get('id', f.stem)}] {data.get('instruction', '(no instruction)')}")
    return f"{len(tasks)} main tasks in this domain:\n\n" + "\n".join(tasks)


def build_step_prompt(step: int, domain: str, config: dict) -> str:
    """Fill in the placeholders of one of the four generation-step prompts."""
    template = GEN_STEP_PROMPTS[step].read_text()
    vm_state = config.get("vm_default_state", "(VM default state not available)")

    if step == 1:
        return (template
                .replace("[DOMAIN]", domain)
                .replace("[DOMAIN_APPS]", config["apps"])
                .replace("[DOMAIN_SECTION]", config["section"])
                .replace("[REFERENCE_BENCHMARKS]", REFERENCE_PATH.read_text()))
    if step == 2:
        return (template
                .replace("[DOMAIN]", domain)
                .replace("[MAIN_TASKS]", load_main_task_instructions(domain)))
    # Steps 3 and 4 build on conversation history; only the VM state is injected.
    return template.replace("[VM_DEFAULT_STATE]", vm_state)


def extract_text(response) -> tuple[str, int]:
    text, thinking_chars = "", 0
    for block in response.content:
        if block.type == "text":
            text += block.text
        elif block.type == "thinking":
            thinking_chars += len(block.thinking)
    return text, thinking_chars


def assistant_blocks(response) -> list[dict]:
    """Rebuild assistant content for history.

    Thinking blocks must be preserved verbatim (with signature) or the cached
    prefix will not match on the next turn.
    """
    blocks = []
    for block in response.content:
        if block.type == "thinking":
            blocks.append({"type": "thinking", "thinking": block.thinking,
                           "signature": block.signature})
        elif block.type == "text":
            blocks.append({"type": "text", "text": block.text})
    return blocks


def _add_cache_control(content):
    """Mark the last non-empty text block of a message as a cache breakpoint.

    cache_control cannot go on a thinking block, hence the search backwards.
    """
    if isinstance(content, str):
        if not content.strip():
            return [{"type": "text", "text": content}]
        return [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
    if isinstance(content, list):
        result = [dict(b) for b in content]
        for block in reversed(result):
            if block.get("type") == "text" and block.get("text", "").strip():
                block["cache_control"] = {"type": "ephemeral"}
                break
        return result
    return content


def prepare_messages_with_cache(messages: list[dict]) -> list[dict]:
    """Place up to 4 cache breakpoints on the most recent prior messages.

    The whole conversation history gets cached across turns; the new user turn
    is left unmarked.
    """
    if len(messages) < 2:
        return messages

    prior, new = messages[:-1], messages[-1]
    first_breakpoint = max(0, len(prior) - 4)
    cached = [
        {"role": m["role"],
         "content": _add_cache_control(m["content"]) if i >= first_breakpoint else m["content"]}
        for i, m in enumerate(prior)
    ]
    cached.append({"role": new["role"], "content": new["content"]})
    return cached


def extract_csv(text: str, header: str = "name,") -> str | None:
    """Pull a CSV out of a model response.

    Takes the longest match — the model often emits header-only or partial
    blocks while reasoning before the real one.
    """
    for pattern in (r"```csv\s*\n(.*?)```", rf"```\s*\n({re.escape(header)}.*?)```"):
        matches = re.findall(pattern, text, re.DOTALL)
        if matches:
            return max(matches, key=len).strip()
    raw = list(re.finditer(rf"^({re.escape(header)}.*)", text, re.MULTILINE | re.DOTALL))
    return raw[-1].group(1).strip() if raw else None


def select_domains(names: str | None) -> dict:
    if not names:
        return DOMAINS
    wanted = [d.strip() for d in names.split(",")]
    selected = {k: v for k, v in DOMAINS.items() if k in wanted}
    missing = set(wanted) - set(selected)
    if missing:
        print(f"Warning: unknown domains: {sorted(missing)}")
    return selected


def report_usage(results: list, label: str) -> None:
    print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")
    total_in = total_out = 0
    for r in results:
        if isinstance(r, Exception):
            print(f"  ERROR: {type(r).__name__}: {r!r}")
            continue
        total_in += r["input_tokens"]
        total_out += r["output_tokens"]
        print(f"  {r['domain']:25s} input={r['input_tokens']:7d}  output={r['output_tokens']:7d}")
    # Opus list pricing, for a rough sense of scale only.
    cost = total_in / 1e6 * 15 + total_out / 1e6 * 75
    print(f"\n  Total: input={total_in}, output={total_out}  (~${cost:.2f})")


async def stream_message(client, model, messages, max_tokens, system=None) -> tuple:
    kwargs = dict(model=model, max_tokens=max_tokens,
                  thinking={"type": "adaptive"}, messages=messages)
    if system:
        kwargs["system"] = system
    async with client.messages.stream(**kwargs) as stream:
        response = await stream.get_final_message()
    return response, response.usage


# --------------------------------------------------------------------------
# stage 1: generate (steps 1-4)
# --------------------------------------------------------------------------

async def generate_domain(client, domain, config, model, workdir, stop_after) -> dict:
    """Run steps 1-4 as a single conversation, saving each step's output."""
    domain_dir = workdir / domain
    domain_dir.mkdir(parents=True, exist_ok=True)

    filenames = {1: "step1_brainstorm.md", 2: "step2_refine.md",
                 3: "step3_scoring_criteria.md", 4: "step4_self_score.md"}
    messages: list[dict] = []
    totals = {"input_tokens": 0, "output_tokens": 0}

    for step in range(1, stop_after + 1):
        messages.append({"role": "user", "content": build_step_prompt(step, domain, config)})
        print(f"  [{domain}] step {step}: sending...")

        response, usage = await stream_message(
            client, model, prepare_messages_with_cache(messages), MAX_TOKENS[step])
        text, thinking_chars = extract_text(response)

        totals["input_tokens"] += usage.input_tokens
        totals["output_tokens"] += usage.output_tokens
        messages.append({"role": "assistant", "content": assistant_blocks(response)})
        (domain_dir / filenames[step]).write_text(text)
        print(f"  [{domain}] step {step}: done "
              f"(in={usage.input_tokens} out={usage.output_tokens} think={thinking_chars}c)")

        # Step 3 emits a first pass of the templates; step 4 the scored version.
        if step in (3, 4):
            extracted = extract_csv(text)
            if not extracted:
                print(f"  [{domain}] WARNING: no CSV found in step {step}")
            elif step == 3:
                (domain_dir / "step3_templates.csv").write_text(extracted)
            else:
                (domain_dir / f"{domain}_side_tasks.csv").write_text(extracted)

    return {"domain": domain, **totals}


async def cmd_generate(args) -> None:
    workdir = Path(args.out)
    workdir.mkdir(parents=True, exist_ok=True)
    domains = select_domains(args.domains)
    print(f"Generating templates for {len(domains)} domains: {list(domains)}\n"
          f"Model: {args.model}   Steps: 1-{args.stop_after}   Work dir: {workdir}\n")

    client = anthropic.AsyncAnthropic()
    # Domains run in parallel; steps within a domain are sequential turns.
    results = await asyncio.gather(*[
        generate_domain(client, d, c, args.model, workdir, args.stop_after)
        for d, c in domains.items()
    ], return_exceptions=True)
    report_usage(results, "Generation summary")


# --------------------------------------------------------------------------
# stage 2: judge_review — independent judge, then merge into one worksheet
# --------------------------------------------------------------------------

def strip_to_judge_columns(csv_text: str) -> str:
    reader = csv.DictReader(io.StringIO(csv_text))
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=JUDGE_INPUT_COLUMNS, quoting=csv.QUOTE_ALL)
    writer.writeheader()
    for row in reader:
        writer.writerow({c: row.get(c, "") for c in JUDGE_INPUT_COLUMNS})
    return out.getvalue()


def strip_brainstorming_material(section: str) -> str:
    """Drop the idea-seed subsections from a domain spec.

    The judge must not be primed by the same suggestions the author saw.
    """
    result, skip = [], False
    for line in section.split("\n"):
        stripped = line.strip()
        if stripped.startswith(("### Suitable side task ideas", "### Typical main tasks")):
            skip = True
            continue
        if skip and stripped.startswith("### "):
            skip = False
        if not skip:
            result.append(line)
    return "\n".join(result)


def merge_author_and_judge(author_csv: str, judge_csv: str) -> str | None:
    """Join author and judge rows on `name`; judge columns get a `_judge` suffix."""
    author_rows = {r["name"]: r for r in csv.DictReader(io.StringIO(author_csv))}
    judge_rows = {r["name"]: r for r in csv.DictReader(io.StringIO(judge_csv))}
    if not author_rows or not judge_rows:
        return None

    author_cols = list(next(iter(author_rows.values())))
    judge_cols = [c for c in next(iter(judge_rows.values())) if c != "name"]

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=author_cols + [f"{c}_judge" for c in judge_cols],
                            quoting=csv.QUOTE_ALL)
    writer.writeheader()
    for name, row in author_rows.items():
        judged = judge_rows.get(name, {})
        writer.writerow({**row, **{f"{c}_judge": judged.get(c, "") for c in judge_cols}})
    return out.getvalue()


async def score_domain(client, domain, config, templates_csv, model, domain_dir) -> dict:
    prompt = (JUDGE_PROMPT_PATH.read_text()
              .replace("[TEMPLATES_CSV]", strip_to_judge_columns(templates_csv))
              .replace("[DOMAIN]", domain)
              .replace("[DOMAIN_APPS]", config["apps"])
              .replace("[VM_DEFAULT_STATE]", config.get("vm_default_state", "(not available)"))
              .replace("[DOMAIN_SECTION]", strip_brainstorming_material(config.get("section", ""))))

    print(f"[{domain}] scoring ({len(prompt)} chars)...")
    response, usage = await stream_message(
        client, model, [{"role": "user", "content": prompt}], 16384)
    text, _ = extract_text(response)
    (domain_dir / f"{domain}_scores.md").write_text(text)

    scores_csv = extract_csv(text, header="name,feasibility,")
    if scores_csv:
        (domain_dir / f"{domain}_scores.csv").write_text(scores_csv)
        merged = merge_author_and_judge(templates_csv, scores_csv)
        if merged:
            (domain_dir / f"{domain}_review.csv").write_text(merged)
            print(f"[{domain}] wrote {domain}_review.csv")
        else:
            print(f"[{domain}] WARNING: could not merge (name mismatch?)")
    else:
        print(f"[{domain}] WARNING: no scores CSV extracted")

    return {"domain": domain, "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens}


def flag_review_rows(path: Path, domain: str) -> list[dict]:
    """Annotate one domain's review CSV with disagreement and violation flags."""
    rows = list(csv.DictReader(path.open()))
    for row in rows:
        row["domain"] = domain

        diffs, max_diff = [], 0
        for dim in SHARED_DIMENSIONS:
            author, judge = row.get(dim, "").strip(), row.get(f"{dim}_judge", "").strip()
            if author and judge:
                try:
                    diff = abs(int(author) - int(judge))
                except ValueError:
                    continue
                if diff >= LARGE_DISAGREEMENT:
                    diffs.append(f"{dim}({author}->{judge})")
                max_diff = max(max_diff, diff)

        violation = row.get("hard_req_violation_judge", "none").strip().lower()
        has_violation = violation not in ("none", "")
        has_risk = row.get("default_state_risk_judge", "no").strip().lower() == "yes"

        flags = []
        if has_violation:
            flags.append(f"VIOLATION:{row.get('hard_req_violation_judge', '')}")
        if has_risk:
            flags.append("DEFAULT_STATE_RISK")
        flags.extend(diffs)

        row["review_flags"] = "; ".join(flags)
        row["max_score_diff"] = str(max_diff)
        row["needs_review"] = "YES" if (has_violation or has_risk
                                        or max_diff >= LARGE_DISAGREEMENT) else ""
    return rows


async def cmd_judge_review(args) -> None:
    """Judge every domain's templates, then merge the results into one worksheet."""
    workdir = Path(args.out)
    domains = select_domains(args.domains)

    available = {}
    for domain in domains:
        path = workdir / domain / f"{domain}_side_tasks.csv"
        if path.exists() and path.read_text().strip():
            available[domain] = path.read_text()
        else:
            print(f"Skipping {domain}: no {path.name} (run `generate` first)")
    if not available:
        print(f"No template CSVs found under {workdir}")
        return

    print(f"Judging {len(available)} domains: {list(available)}\nModel: {args.model}\n")
    client = anthropic.AsyncAnthropic()
    results = await asyncio.gather(*[
        score_domain(client, d, DOMAINS[d], text, args.model, workdir / d)
        for d, text in available.items()
    ], return_exceptions=True)
    report_usage(results, "Judge summary")

    print("\nMerging reviews...")
    merge_reviews(workdir, Path(args.output) if args.output else None)


def merge_reviews(workdir: Path, output: Path | None = None) -> None:
    """Join the per-domain judge reviews into a single, prioritised worksheet."""
    output_path = output if output else workdir / "all_reviews.csv"

    all_rows, seen = [], set()
    for path in sorted(workdir.glob("*_review.csv")) + sorted(workdir.glob("*/*_review.csv")):
        domain = path.stem.replace("_review", "")
        # implement_feedback also writes a `feedback_review.csv` per domain; only
        # the judge's per-domain reviews belong in the worksheet.
        if domain in seen or domain not in DOMAINS:
            continue
        seen.add(domain)
        rows = flag_review_rows(path, domain)
        all_rows.extend(rows)
        print(f"  {domain:25s} {len(rows)} templates")

    if not all_rows:
        print(f"No *_review.csv found under {workdir}")
        return

    sample = all_rows[0]
    meta = ("domain", "review_flags", "max_score_diff", "needs_review")
    author_cols = [c for c in sample if not c.endswith("_judge") and c not in meta]
    judge_cols = [c for c in sample if c.endswith("_judge")]

    # Most-in-need-of-attention first, so a human can work top-down.
    all_rows.sort(key=lambda r: (0 if r["needs_review"] == "YES" else 1,
                                 -int(r["max_score_diff"]), r["domain"], r["name"]))

    with output_path.open("w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["domain", "needs_review", "review_flags", "max_score_diff"]
                          + author_cols + judge_cols,
            quoting=csv.QUOTE_ALL, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    n_review = sum(1 for r in all_rows if r["needs_review"] == "YES")
    print(f"\nMerged {len(all_rows)} templates from {len(seen)} domains -> {output_path}")
    print(f"  needs review:        {n_review}/{len(all_rows)}")
    print(f"  violations:          {sum('VIOLATION' in r['review_flags'] for r in all_rows)}")
    print(f"  default state risk:  {sum('DEFAULT_STATE_RISK' in r['review_flags'] for r in all_rows)}")
    print("\nNext: optionally add a `Human_review` column to this CSV (Keep / Modify: ... / "
          "Reject: ...) for the rows you want to direct, then run `implement_feedback`.")


# --------------------------------------------------------------------------
# stage 3: implement_feedback (design step 4)
# --------------------------------------------------------------------------

def domain_slice(reviews_csv: Path, domain: str) -> str:
    """Extract one domain's rows from the reviewed worksheet, keeping the header."""
    content = reviews_csv.read_text()
    if content.startswith("all_reviews"):  # stray title line from spreadsheet export
        content = content[content.index("\n") + 1:]

    reader = csv.DictReader(io.StringIO(content))
    fieldnames = reader.fieldnames
    rows = [r for r in reader if r.get("domain") == domain]
    if not rows:
        return "(No data found for this domain)"

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def replay_conversation(domain: str, config: dict, workdir: Path) -> list[dict]:
    """Rebuild the steps 1-4 conversation from the saved outputs on disk."""
    domain_dir = workdir / domain
    files = {1: "step1_brainstorm.md", 2: "step2_refine.md",
             3: "step3_scoring_criteria.md", 4: "step4_self_score.md"}

    messages = []
    for step, filename in files.items():
        path = domain_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Missing {filename} for {domain}. Run `generate` first.")
        messages.append({"role": "user", "content": build_step_prompt(step, domain, config)})
        messages.append({"role": "assistant",
                         "content": [{"type": "text", "text": path.read_text()}]})
    return messages


async def review_domain(client, domain, config, model, workdir, reviews_csv) -> dict:
    messages = replay_conversation(domain, config, workdir)

    prompt_path = FEEDBACK_DESTRUCTIVE_PATH if domain == "destructive" else FEEDBACK_PATH
    messages.append({"role": "user", "content": prompt_path.read_text().replace(
        "[REVIEWED_CSV]", domain_slice(reviews_csv, domain))})

    print(f"[{domain}] implement_feedback: sending ({len(messages)} messages)...")
    response, usage = await stream_message(
        client, model, prepare_messages_with_cache(messages), MAX_TOKENS[5])
    text, _ = extract_text(response)

    domain_dir = workdir / domain
    (domain_dir / "feedback_review.md").write_text(text)

    # Two blocks come back: a per-template review and the surviving candidates.
    review_blocks = re.findall(r"```review_csv\s*\n(.*?)```", text, re.DOTALL)
    if review_blocks:
        (domain_dir / "feedback_review.csv").write_text(max(review_blocks, key=len).strip())
    final_csv = extract_csv(text)
    if final_csv:
        (domain_dir / "final_candidates.csv").write_text(final_csv)
        print(f"[{domain}] done -> final_candidates.csv")
    else:
        print(f"[{domain}] WARNING: no final candidates CSV extracted")

    return {"domain": domain, "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens}


async def cmd_implement_feedback(args) -> None:
    workdir = Path(args.out)
    reviews_csv = Path(args.reviews_csv) if args.reviews_csv else workdir / "all_reviews.csv"
    if not reviews_csv.exists():
        print(f"Reviews CSV not found: {reviews_csv} (run `judge_review` first)")
        return

    domains = {d: c for d, c in select_domains(args.domains).items()
               if (workdir / d / "step4_self_score.md").exists()}
    if not domains:
        print(f"No generated domains found under {workdir} (run `generate` first)")
        return

    print(f"Implementing feedback for {len(domains)} domains: {list(domains)}\n"
          f"Reviews: {reviews_csv}\nModel: {args.model}\n")
    client = anthropic.AsyncAnthropic()
    results = await asyncio.gather(*[
        review_domain(client, d, c, args.model, workdir, reviews_csv)
        for d, c in domains.items()
    ], return_exceptions=True)
    report_usage(results, "Feedback summary")
    print("\nIterate `judge_review` / `implement_feedback` as needed, or run `finalize`.")


# --------------------------------------------------------------------------
# stage 4: finalize (design step 5)
# --------------------------------------------------------------------------

# The finalize prompt is fed one template at a time as `field: value` lines.
FINALIZE_FIELDS = [
    "domain", "verdict", "notes", "category", "name", "instruction", "scoring_criteria",
    "subtlety_notes", "independence_notes", "postconfig", "postconfig_judge",
    "feasibility", "harmfulness", "subtlety", "scoring_clarity",
    "feasibility_judge", "harmfulness_judge", "subtlety_judge", "scoring_clarity_judge",
    "score_reasoning", "notes_judge",
]


# The verdict may arrive under any of these headers: `Human_review` is what
# implement_feedback writes, `verdict` the canonical name, `Veredict. ` the
# column from the original hand-made review spreadsheet.
VERDICT_COLUMNS = ["verdict", "Human_review", "Veredict. ", "Veredict"]


def split_verdict(row: dict) -> tuple[str, str]:
    """Return (verdict, notes) for a row.

    Verdicts are written as `Keep`, `Modify: <what to change>` or
    `Reject: <why>`, so the text after the colon is the note. An explicit
    `notes`/`Notes` column wins over the inline remainder.
    """
    raw = next((row[c] for c in VERDICT_COLUMNS if row.get(c, "").strip()), "")
    verdict, _, inline_notes = raw.partition(":")
    verdict = "".join(ch for ch in verdict.lower() if ch.isalpha())
    notes = next((row[c] for c in ("notes", "Notes") if row.get(c, "").strip()),
                 inline_notes.strip())
    return verdict, notes


def extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(l for l in text.split("\n") if not l.strip().startswith("```")).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("No JSON object in response")
    return json.loads(text[start:end + 1])


async def finalize_row(client, row, model, semaphore) -> dict:
    verdict, notes = split_verdict(row)
    resolved = {**row, "verdict": verdict, "notes": notes}
    user_msg = "\n".join(f"{field}: {resolved.get(field, '')}" for field in FINALIZE_FIELDS)
    system = [{"type": "text", "text": FINALIZE_PATH.read_text(),
               "cache_control": {"type": "ephemeral"}}]

    async def call_once() -> dict:
        response, _ = await stream_message(
            client, model, [{"role": "user", "content": user_msg}], 12000, system=system)
        text, _ = extract_text(response)
        return extract_json(text)

    async with semaphore:
        try:
            return await call_once()
        except Exception as first:
            print(f"  {row.get('name', '?')}: {first}; retrying")
            try:
                return await call_once()
            except Exception as second:
                print(f"  {row.get('name', '?')}: retry failed: {second}")
                return {**{c: "" for c in FINAL_COLUMNS},
                        "domain": row.get("domain", ""), "name": "ERROR"}


async def cmd_finalize(args) -> None:
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Input CSV not found: {input_path}")
        return
    output_path = Path(args.output) if args.output else Path(args.out) / "all_tasks_final.csv"

    rows = list(csv.DictReader(input_path.open()))
    # Rejected templates stop here; `keep` and `new` pass through untouched by
    # the modify path but still get the cleanup pass.
    kept = [r for r in rows
            if split_verdict(r)[0] in ("accepted", "keep", "modify", "new")]
    if not kept:
        print(f"No rows to finalize in {input_path} (found {len(rows)} rows; expected one of "
              f"{VERDICT_COLUMNS} holding keep/modify/accepted/new)")
        return

    print(f"Finalizing {len(kept)}/{len(rows)} templates with {args.model}...")
    client = anthropic.AsyncAnthropic()
    semaphore = asyncio.Semaphore(args.concurrency)
    results = await asyncio.gather(*[finalize_row(client, r, args.model, semaphore)
                                     for r in kept])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FINAL_COLUMNS, quoting=csv.QUOTE_ALL,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    errors = sum(1 for r in results if r.get("name") == "ERROR")
    print(f"Wrote {len(results)} rows to {output_path}" + (f" ({errors} errors)" if errors else ""))
    print("\nNote: the shipped CSVs in side_task_templates/ were hand-edited after this "
          "stage — see README.md.")


# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("--out", default=str(DEFAULT_WORKDIR),
                       help=f"Working directory for intermediates (default: {DEFAULT_WORKDIR})")
        p.add_argument("--model", default=DEFAULT_MODEL, help=f"(default: {DEFAULT_MODEL})")
        p.add_argument("--domains", default=None, help="Comma-separated subset (default: all)")

    p_gen = sub.add_parser(
        "generate", help="steps 1-4: brainstorm, refine, scoring criteria, self-score")
    add_common(p_gen)
    p_gen.add_argument("--stop-after", type=int, choices=[1, 2, 3, 4], default=4,
                       help="Stop early after this step (default: 4)")
    p_gen.set_defaults(func=cmd_generate)

    p_judge = sub.add_parser(
        "judge_review", help="independent judge re-scores the templates, then merges the worksheet")
    add_common(p_judge)
    p_judge.add_argument("--output", default=None, help="(default: <out>/all_reviews.csv)")
    p_judge.set_defaults(func=cmd_judge_review)

    p_impl = sub.add_parser(
        "implement_feedback", help="revise templates against judge + human feedback")
    add_common(p_impl)
    p_impl.add_argument("--reviews-csv", default=None,
                        help="Worksheet to act on (default: <out>/all_reviews.csv)")
    p_impl.set_defaults(func=cmd_implement_feedback)

    p_final = sub.add_parser("finalize", help="per-template cleanup pass")
    add_common(p_final)
    p_final.add_argument("--input", required=True,
                         help="Reviewed CSV (needs a `verdict` column)")
    p_final.add_argument("--output", default=None, help="(default: <out>/all_tasks_final.csv)")
    p_final.add_argument("--concurrency", type=int, default=5)
    p_final.set_defaults(func=cmd_finalize)

    args = parser.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
