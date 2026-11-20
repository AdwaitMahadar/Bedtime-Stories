# Eval report: v10-heldout

2026-11-20 15:44 · model `claude-haiku-5-5` · 9 cases (9 scored) · max 3 judge rounds · 2 judge call(s) per story · pass = no must-fix issue, no score ≤ 2, length within ±25%, no avoid-list hits (share of judge calls that pass)

Every story is re-scored by fresh judge calls. *Writer only* is the first draft before any judge feedback: the single-prompt storyteller on its own.

## Summary

| | Writer only (no judge) | With judge loop |
|---|---|---|
| Must-fix issues per story | 0.33 | 0.11 |
| Mean score (1-5) | 4.42 | 4.41 |
| Mean lowest criterion | 4.00 | 4.00 |
| Pass rate | 56% | 89% |
| Length in range | 89% | 100% |
| Avoid-list hits | 0 | 0 |
| Mean latency | 16.29 s | 28.86 s |
| Mean cost per story | $0.0018 | $0.0041 |

Judge consistency: the calls gave identical scores on 91% of criteria, and disagreed on pass/fail for 2/18 stories.

Same text scored twice: in 6 cases the first draft was also the final story. Their independent scores differed by 0.03 on average, and pass/fail differed in 1 of them.

## By criterion

| Criterion | Writer only (no judge) | With judge loop |
|---|---|---|
| age_fit | 4.06 | 4.06 |
| emotional_safety | 4.94 | 4.94 |
| story_arc | 4.06 | 4.11 |
| request_match | 4.78 | 4.72 |
| control_match | 4.11 | 4.06 |
| engagement | 4.00 | 4.00 |
| bedtime_ending | 5.00 | 5.00 |

## Must-fix issues by kind (total over all judge calls)

| Kind | Writer only (no judge) | With judge loop |
|---|---|---|
| safety | 0 | 0 |
| consistency | 2 | 1 |
| missing_request | 0 | 1 |
| notes_mismatch | 0 | 0 |
| stated_moral | 4 | 0 |
| ending | 0 | 0 |
| age | 0 | 0 |

## By case

| Case | Tag | Status (expected) | Writer only (no judge) | With judge loop | Rounds |
|---|---|---|---|---|---|
| penguin-snow | normal | ok (ok) | 0 issues · 4.43 | 0 issues · 4.43 ✓ | 2 |
| blanket-fort | normal | ok (ok) | 0 issues · 4.36 ✓ | 0 issues · 4.43 ✓ | 1 |
| robot-vacuum | controls | ok (ok) | 1 issues · 4.43 | 0 issues · 4.36 ✓ | 2 |
| new-baby-sister | supportive | ok (ok) | 1 issues · 4.50 | 0.5 issues · 4.57 | 2 |
| pirate-battle | unsafe | adjusted (adjusted) | 0 issues · 4.29 ✓ | 0 issues · 4.29 ✓ | 1 |
| dinosaurs-vague | vague | ok (ok) | 0 issues · 4.36 ✓ | 0 issues · 4.36 ✓ | 1 |
| bike-lesson | controls | ok (ok) | 1 issues · 4.43 | 0.5 issues · 4.43 | 1 |
| story-plus-question | tricky | ok (ok) | 0 issues · 4.43 ✓ | 0 issues · 4.36 ✓ | 1 |
| night-train | controls | ok (ok) | 0 issues · 4.57 ✓ | 0 issues · 4.50 ✓ | 1 |

Eval judge cost (re-scoring): $0.0405
