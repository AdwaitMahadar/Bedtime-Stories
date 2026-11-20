"""Judge check: does the judge catch problems we planted on purpose, without false alarms?

    python -m eval.judge_check                  # every case, 2 judge calls each
    python -m eval.judge_check --samples 3      # more calls per case
    python -m eval.judge_check --label v9       # name the results folder

`judge_cases.json` holds short, hand-written stories. Each "planted" story contains exactly one
must-fix problem of a known kind (a stated moral, a pronoun change, a title naming someone who isn't
in the story, a secret kept from parents, a child at the stove alone, death described as sleep, a
missing requested element, notes that don't match, a cliffhanger ending, vocabulary far too hard).
"Clean" stories contain none; two of them test over-reach (ordinary independence, an animal
climbing, a softened spooky request).

For each judge call the report records whether the planted kind was flagged ("caught, right kind"),
whether the story was blocked at all by the app's pass rule ("caught"), and for clean stories whether
it was blocked anyway ("false alarm"). Outputs go to `eval/results/judge-check-<label>/`.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from statistics import mean

from storyteller import stages
from storyteller.checks import run_checks
from storyteller.llm import LLM, check_api_key, load_api_key
from storyteller.models import ChildProfile, Draft, StoryControls, StoryRequest, TraceEvent
from storyteller.pipeline import gate

EVAL_DIR = Path(__file__).parent


def load_cases(path: Path = EVAL_DIR / "judge_cases.json") -> list[dict]:
    return json.loads(path.read_text())["cases"]


def check_case(llm, case: dict, samples: int, trace: list[TraceEvent]) -> dict:
    profile = ChildProfile(**case["profile"]) if case.get("profile") else None
    request = StoryRequest(text=case["request"], profile=profile, controls=StoryControls(**case["controls"]))
    draft = Draft(text=case["text"], title=case["title"], notes=case.get("notes", []))
    checks = run_checks(draft, request.controls, request.age, request.avoid)
    calls = []
    for _ in range(samples):
        verdict = stages.judge(
            llm, request, draft, checks, case.get("adjustments", []), trace, sensitive_topic=case.get("sensitive_topic")
        )
        kinds = [issue.kind for issue in verdict.issues]
        calls.append(
            {
                "kinds": kinds,
                "issues": [issue.model_dump() for issue in verdict.issues],
                "blocked": bool(gate(verdict, checks)),
                "right_kind": case["planted"] in kinds if case["planted"] else None,
                "min_score": verdict.min_score,
            }
        )
    return {"id": case["id"], "planted": case["planted"], "note": case["note"], "calls": calls}


def summarize(results: list[dict]) -> dict:
    planted = [c for r in results if r["planted"] for c in r["calls"]]
    clean = [c for r in results if not r["planted"] for c in r["calls"]]
    return {
        "planted_calls": len(planted),
        "caught_right_kind": mean(c["right_kind"] for c in planted) if planted else None,
        "caught": mean(c["blocked"] for c in planted) if planted else None,
        "clean_calls": len(clean),
        "false_alarms": mean(c["blocked"] for c in clean) if clean else None,
    }


def _pct(value) -> str:
    return "—" if value is None else f"{100 * value:.0f}%"


def render_report(results: list[dict], label: str, samples: int) -> str:
    s = summarize(results)
    lines = [
        f"# Judge check: {label}",
        "",
        f"{datetime.now():%Y-%m-%d %H:%M} · {len(results)} hand-written stories · {samples} judge call(s) each",
        "",
        "| | Result |",
        "|---|---|",
        f"| Planted problem caught with the right kind | {_pct(s['caught_right_kind'])} of {s['planted_calls']} calls |",
        f"| Planted story blocked (any must-fix issue or a score of 2 or less) | {_pct(s['caught'])} |",
        f"| Clean story blocked anyway (false alarm) | {_pct(s['false_alarms'])} of {s['clean_calls']} calls |",
        "",
        "| Story | Planted | Flagged in each call |",
        "|---|---|---|",
    ]
    for r in results:
        flagged = " / ".join(", ".join(c["kinds"]) or "nothing" for c in r["calls"])
        lines.append(f"| {r['id']} | {r['planted'] or 'none (clean)'} | {flagged} |")
    lines += ["", "## What the judge quoted", ""]
    for r in results:
        lines.append(f"**{r['id']}** ({r['note']})")
        for i, call in enumerate(r["calls"], 1):
            for issue in call["issues"]:
                lines.append(f'- call {i}, {issue["kind"]}: "{issue["quote"][:160]}"')
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--samples", type=int, default=2, help="judge calls per story")
    parser.add_argument("--label", default=datetime.now().strftime("%Y%m%d-%H%M"))
    args = parser.parse_args(argv)

    key = load_api_key()
    status = check_api_key(key)
    if not status.ok:
        print(f"Can't run: {status.message}", file=sys.stderr)
        return 1
    llm = LLM(key)
    trace: list[TraceEvent] = []
    results = [check_case(llm, case, args.samples, trace) for case in load_cases()]
    out_dir = EVAL_DIR / "results" / f"judge-check-{args.label}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=2))
    report = render_report(results, args.label, args.samples)
    (out_dir / "report.md").write_text(report + f"\nJudge cost: ${sum(e.cost_usd for e in trace):.4f}\n")
    print(report.split("## What the judge quoted")[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
