"""Evaluate what the judge loop adds on top of a single well-written prompt.

    python -m eval.run_eval                  # all cases
    python -m eval.run_eval --cases a,b      # selected cases
    python -m eval.run_eval --label v5       # name the results folder
    python -m eval.run_eval --set heldout    # the held-out requests (never used for tuning)

For every case the pipeline runs once, and two of its stories are scored:
- **first draft**: the writer's story before any judge feedback. This is the single-prompt
  storyteller on its own, so it doubles as the baseline.
- **final**: the draft chosen after the judge-and-revise loop.

Each story is scored by **fresh** judge calls with the same prompt (`--judge-samples`, default 2).
The headline number is **must-fix issues per story** (safety, consistency, missing request, notes
mismatch, stated moral, lively ending, wrong age level), averaged over calls. A story's pass rate is
the share of calls that found no issue and no score of 2 or less (with code checks passing). Scores
are averaged per criterion. Agreement between calls is reported as a measure of judge consistency. The judge scores from inside the loop are not reused:
the loop keeps the best-scoring draft, so its own scores are biased upward.
Outputs go to `eval/results/<label>/`: report.md, stories.md, results.json.

Results up to v4 also compared an older planner -> writer design, and up to v7 a story passed on
"every score >= 4"; see docs/PROMPT_CHANGELOG.md.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import get_args

from storyteller import config, stages
from storyteller.checks import check_failures, run_checks
from storyteller.llm import LLM, LLMError, check_api_key, load_api_key
from storyteller.models import CRITERIA, ChildProfile, Draft, IssueKind, StoryControls, StoryRequest, TraceEvent
from storyteller.pipeline import StoryPipeline

EVAL_DIR = Path(__file__).parent
VARIANTS = ("first_draft", "final")
LABELS = {"first_draft": "Writer only (no judge)", "final": "With judge loop"}


def load_cases(path: Path, only: set[str] | None) -> list[dict]:
    cases = json.loads(path.read_text())["cases"]
    return [c for c in cases if not only or c["id"] in only]


def to_request(case: dict) -> StoryRequest:
    profile = ChildProfile(**case["profile"]) if case.get("profile") else None
    return StoryRequest(text=case["request"], profile=profile, controls=StoryControls(**case.get("controls", {})))


def score(
    llm: LLM,
    request: StoryRequest,
    draft: Draft,
    trace: list[TraceEvent],
    samples: int = 1,
    adjustments: list[str] | None = None,
    language: str = "en",
    sensitive_topic: str | None = None,
) -> dict:
    """Code checks plus `samples` fresh, independent judge calls for one story.

    The judge gets the request check's adjustments and the story's parent notes, exactly as the
    in-app judge does, so a deliberately softened request is judged the same way in both places.
    """
    checks = run_checks(draft, request.controls, request.age, request.avoid, language)
    verdicts = [
        stages.judge(
            llm, request, draft, checks, adjustments=adjustments or [], trace=trace, sensitive_topic=sensitive_topic
        )
        for _ in range(samples)
    ]
    per_criterion = {c: mean(getattr(v, c).score for v in verdicts) for c in CRITERIA}
    measured_ok = not check_failures(checks)

    def passes(v) -> bool:  # the eval's fixed pass definition, for one judge call
        return measured_ok and not v.issues and v.min_score > config.FAIL_MAX_SCORE

    result = {
        "title": draft.title,
        "text": draft.text,
        "notes": draft.notes,
        "checks": checks.model_dump(),
        "verdict": verdicts[0].model_dump(),
        "verdicts": [v.model_dump() for v in verdicts],
        "scores": per_criterion,
        "mean": round(mean(per_criterion.values()), 2),
        "min": round(min(per_criterion.values()), 2),
        "issues": mean(len(v.issues) for v in verdicts),
        "issue_kinds": [i.kind for v in verdicts for i in v.issues],
        "passed": mean(passes(v) for v in verdicts),  # share of judge calls that pass the story
    }
    if samples > 1:
        same = [len({getattr(v, c).score for v in verdicts}) == 1 for c in CRITERIA]
        result["agreement"] = sum(same) / len(same)  # share of criteria where every call gave the same score
        result["pass_flip"] = len({passes(v) for v in verdicts}) > 1  # calls disagree on pass/fail
    return result


def _cost(trace: list[TraceEvent]) -> float:
    return sum(e.cost_usd for e in trace)


def run_case(llm: LLM, case: dict, max_rounds: int, judge_samples: int = 1) -> dict:
    request = to_request(case)
    result: dict = {
        "id": case["id"],
        "tag": case["tag"],
        "request": case["request"],
        "expected_status": case.get("expected_status"),
        "variants": {},
    }
    judge_trace: list[TraceEvent] = []
    try:
        started = time.monotonic()
        session = StoryPipeline(llm, max_rounds=max_rounds).create(request)
        latency = round(time.monotonic() - started, 1)
        brief = session.brief
        result.update(status=brief.status, adjustments=brief.adjustments, language=brief.language)
        if brief.status == "out_of_scope":
            result["redirect_message"] = brief.redirect_message
            return result

        version = session.current
        result["rounds"] = len(version.rounds)
        result["in_loop_failures"] = [r.failures + r.soft_failures for r in version.rounds]
        writer = [e for e in session.trace if e.step == "writer"]
        first = score(
            llm,
            request,
            version.rounds[0].draft,
            judge_trace,
            judge_samples,
            brief.adjustments,
            brief.language,
            brief.sensitive_topic,
        )
        first.update(latency_s=round(sum(e.latency_ms for e in writer) / 1000, 1), cost_usd=_cost(writer))
        final = score(
            llm,
            request,
            version.best.draft,
            judge_trace,
            judge_samples,
            brief.adjustments,
            brief.language,
            brief.sensitive_topic,
        )
        final.update(latency_s=latency, cost_usd=_cost(session.trace))
        result["variants"]["first_draft"] = first
        result["variants"]["final"] = final
    except LLMError as error:
        result["error"] = str(error)
    finally:
        result["judge_cost_usd"] = _cost(judge_trace)
    return result


# --- Report ------------------------------------------------------------------------------------


def _avg(values: list[float]) -> str:
    return f"{mean(values):.2f}" if values else "—"


def _pct(flags: list[bool]) -> str:
    return f"{100 * sum(flags) / len(flags):.0f}%" if flags else "—"


def _scored(results: list[dict]) -> list[dict]:
    """Cases with every variant scored (out-of-scope and errored cases are left out)."""
    return [r for r in results if all(v in r["variants"] for v in VARIANTS)]


def summarize(results: list[dict]) -> dict:
    scored = _scored(results)
    summary = {}
    for variant in VARIANTS:
        rows = [r["variants"][variant] for r in scored]
        summary[variant] = {
            "mean": _avg([x["mean"] for x in rows]),
            "min": _avg([x["min"] for x in rows]),
            "pass_rate": f"{100 * mean(x['passed'] for x in rows):.0f}%" if rows else "—",
            "issues": _avg([x["issues"] for x in rows]),
            "issue_kinds": {k: sum(x["issue_kinds"].count(k) for x in rows) for k in get_args(IssueKind)},
            "length_ok": _pct([x["checks"]["length_ok"] for x in rows]),
            "avoid_hits": sum(len(x["checks"]["avoid_hits"]) for x in rows),
            "criteria": {c: _avg([x["scores"][c] for x in rows]) for c in CRITERIA},
            "latency": _avg([x["latency_s"] for x in rows if "latency_s" in x]),
            "cost": mean([x["cost_usd"] for x in rows if "cost_usd" in x] or [0]),
        }
    sampled = [x for r in scored for x in r["variants"].values() if "agreement" in x]
    # When the first draft passes, the final story is the same text, scored again independently:
    # the gap between the two scores is pure judge noise.
    same = [r for r in scored if r["variants"]["first_draft"]["text"] == r["variants"]["final"]["text"]]
    summary["same_text"] = {
        "n": len(same),
        "mean_gap": _avg([abs(r["variants"]["first_draft"]["mean"] - r["variants"]["final"]["mean"]) for r in same]),
        "pass_flips": sum(r["variants"]["first_draft"]["passed"] != r["variants"]["final"]["passed"] for r in same),
    }
    summary["agreement"] = f"{100 * mean(x['agreement'] for x in sampled):.0f}%" if sampled else None
    summary["pass_flips"] = f"{sum(x['pass_flip'] for x in sampled)}/{len(sampled)}" if sampled else None
    return summary


def render_report(results: list[dict], label: str, max_rounds: int, judge_samples: int = 1) -> str:
    s = summarize(results)
    scored = _scored(results)
    lines = [
        f"# Eval report: {label}",
        "",
        f"{datetime.now():%Y-%m-%d %H:%M} · model `{config.MODEL}` · {len(results)} cases "
        f"({len(scored)} scored) · max {max_rounds} judge rounds · {judge_samples} judge call(s) per story · "
        f"pass = no must-fix issue, no score ≤ {config.FAIL_MAX_SCORE}, length within ±{config.LENGTH_TOLERANCE:.0%}, "
        "no avoid-list hits (share of judge calls that pass)",
        "",
        "Every story is re-scored by fresh judge calls. *Writer only* is the first draft before any judge feedback: "
        "the single-prompt storyteller on its own.",
        "",
        "## Summary",
        "",
        "| | " + " | ".join(LABELS[v] for v in VARIANTS) + " |",
        "|---|" + "---|" * len(VARIANTS),
        "| Must-fix issues per story | " + " | ".join(s[v]["issues"] for v in VARIANTS) + " |",
        "| Mean score (1-5) | " + " | ".join(s[v]["mean"] for v in VARIANTS) + " |",
        "| Mean lowest criterion | " + " | ".join(s[v]["min"] for v in VARIANTS) + " |",
        "| Pass rate | " + " | ".join(s[v]["pass_rate"] for v in VARIANTS) + " |",
        "| Length in range | " + " | ".join(s[v]["length_ok"] for v in VARIANTS) + " |",
        "| Avoid-list hits | " + " | ".join(str(s[v]["avoid_hits"]) for v in VARIANTS) + " |",
        "| Mean latency | " + " | ".join(f"{s[v]['latency']} s" if s[v]["cost"] else "—" for v in VARIANTS) + " |",
        "| Mean cost per story | "
        + " | ".join(f"${s[v]['cost']:.4f}" if s[v]["cost"] else "—" for v in VARIANTS)
        + " |",
        "",
        *(
            [
                f"Judge consistency: the calls gave identical scores on {s['agreement']} of criteria, "
                f"and disagreed on pass/fail for {s['pass_flips']} stories.",
                "",
            ]
            if s.get("agreement")
            else []
        ),
        *(
            [
                f"Same text scored twice: in {s['same_text']['n']} cases the first draft was also the final story. "
                f"Their independent scores differed by {s['same_text']['mean_gap']} on average, and pass/fail "
                f"differed in {s['same_text']['pass_flips']} of them.",
                "",
            ]
            if s["same_text"]["n"]
            else []
        ),
        "## By criterion",
        "",
        "| Criterion | " + " | ".join(LABELS[v] for v in VARIANTS) + " |",
        "|---|" + "---|" * len(VARIANTS),
    ]
    for criterion in CRITERIA:
        lines.append(f"| {criterion} | " + " | ".join(s[v]["criteria"][criterion] for v in VARIANTS) + " |")
    lines += [
        "",
        "## Must-fix issues by kind (total over all judge calls)",
        "",
        "| Kind | " + " | ".join(LABELS[v] for v in VARIANTS) + " |",
        "|---|" + "---|" * len(VARIANTS),
    ]
    for kind in get_args(IssueKind):
        lines.append(f"| {kind} | " + " | ".join(str(s[v]["issue_kinds"][kind]) for v in VARIANTS) + " |")
    lines += [
        "",
        "## By case",
        "",
        "| Case | Tag | Status (expected) | " + " | ".join(LABELS[v] for v in VARIANTS) + " | Rounds |",
        "|---|---|---|" + "---|" * len(VARIANTS) + "---|",
    ]
    for r in results:
        status = f"{r.get('status', 'error')} ({r.get('expected_status')})"
        cells = []
        for v in VARIANTS:
            x = r["variants"].get(v)
            cells.append(f"{x['issues']:g} issues · {x['mean']:.2f}{' ✓' if x['passed'] == 1 else ''}" if x else "—")
        lines.append(f"| {r['id']} | {r['tag']} | {status} | " + " | ".join(cells) + f" | {r.get('rounds', '—')} |")
    errors = [r for r in results if r.get("error")]
    if errors:
        lines += ["", "## Errors", ""] + [f"- {r['id']}: {r['error']}" for r in errors]
    judge_cost = sum(r.get("judge_cost_usd", 0) for r in results)
    lines += ["", f"Eval judge cost (re-scoring): ${judge_cost:.4f}", ""]
    return "\n".join(lines)


def render_stories(results: list[dict]) -> str:
    out = ["# Eval stories", ""]
    for r in results:
        out += [f"## {r['id']} ({r['tag']})", "", f"**Request:** {r['request']}", ""]
        if r.get("status"):
            out.append(
                f"**Status:** {r['status']} (expected {r.get('expected_status')}) · language: {r.get('language')}"
            )
        for adjustment in r.get("adjustments", []):
            out.append(f"- adjustment: {adjustment}")
        if r.get("redirect_message"):
            out += ["", f"**Redirect:** {r['redirect_message']}"]
        if r.get("error"):
            out += ["", f"**Error:** {r['error']}"]
        for v in VARIANTS:
            x = r["variants"].get(v)
            if not x:
                continue
            c = x["checks"]
            out += [
                "",
                f"### {LABELS[v]}: *{x['title']}*",
                f"{x['issues']:g} issues · mean {x['mean']:.2f} · passed {x['passed']:.0%} of reviews · "
                f"{c['word_count']} words (target {c['length_target']}) · grade {c['reading_grade']}",
                "",
                *[f"- note: {n}" for n in x.get("notes", [])],
                *[f'- issue ({i["kind"]}): "{i["quote"]}" -> {i["fix"]}' for i in x["verdict"]["issues"]],
                "",
                f"> Judge: {x['verdict']['summary']}",
                "",
                x["text"],
            ]
        out += ["", "---", ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cases", help="comma-separated case ids (default: all)")
    parser.add_argument(
        "--set",
        choices=["dev", "heldout"],
        default="dev",
        help="dev = requests.json (used while tuning); heldout = heldout_requests.json (run once, never tuned on)",
    )
    parser.add_argument("--label", default=datetime.now().strftime("%Y%m%d-%H%M"))
    parser.add_argument("--max-rounds", type=int, default=config.MAX_ROUNDS)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--judge-samples", type=int, default=2, help="fresh judge calls per story, averaged")
    args = parser.parse_args(argv)

    key = load_api_key()
    status = check_api_key(key)
    if not status.ok:
        print(f"Can't run the eval: {status.message}", file=sys.stderr)
        return 1

    only = set(args.cases.split(",")) if args.cases else None
    request_file = "requests.json" if args.set == "dev" else "heldout_requests.json"
    cases = load_cases(EVAL_DIR / request_file, only)
    llm = LLM(key)
    print(f"Running {len(cases)} cases with {args.workers} workers...")
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_case, llm, case, args.max_rounds, args.judge_samples): case["id"] for case in cases}
        for future in as_completed(futures):
            r = future.result()
            results[r["id"]] = r
            final = r["variants"].get("final")
            first = r["variants"].get("first_draft")
            detail = r.get("error") or (
                f"first draft {first['issues']:g} issues -> final {final['issues']:g} in {r['rounds']} review(s)"
                if final and first
                else f"status {r.get('status')}"
            )
            print(f"  ✓ {r['id']}: {detail}", flush=True)

    ordered = [results[c["id"]] for c in cases]
    out_dir = EVAL_DIR / "results" / args.label
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(ordered, indent=2, default=str))
    report = render_report(ordered, args.label, args.max_rounds, args.judge_samples)
    (out_dir / "report.md").write_text(report)
    (out_dir / "stories.md").write_text(render_stories(ordered))
    print(f"\nWrote {out_dir}/report.md, stories.md and results.json")
    print(report.split("## By criterion")[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
