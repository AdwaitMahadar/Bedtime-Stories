# Prompt changelog

Each revision came from reading eval stories and was measured with `python -m eval.run_eval`. Scores are on a 1–5 scale, averaged over 13 scored cases (the off-topic case is redirected, not scored). Every story is re-scored by fresh judge calls. Full reports and stories are in `eval/results/v1` … `v7`.

## How to read the numbers across versions

The way stories are measured changed three times. Each change made the eval more accurate, so **scores from different versions are not directly comparable**. Compare variants within a version, not a number from v1 against v7.

| Version | Measurement change | Effect |
|---|---|---|
| v3 | Judge scores request match on the request's spirit *within bedtime-safe limits* (softening is never penalized) | request_match from v3 on is not comparable with v1–v2 |
| v3 | Each story gets 2 judge calls, averaged per criterion | Stricter (a 3 from either call fails) and less noisy |
| v7 | The eval judge gets the analyzer's adjustments, as the in-app judge does | Removes noise on softened requests |

Apart from these, the judge prompt stayed fixed while the generation prompts changed.

Pass rate, with 13 cases, moves by ±15 points from judge noise alone (v4 shows this: an unchanged prompt went from 54% to 38%). Treat it as a rough signal. Length in range and mean lowest criterion are steadier.

## v1: initial prompts

Prompts: analyzer, planner, writer, judge, reviser and follow-up router, compared against a single-prompt baseline.

- **Result:** the judge loop raised the pass rate from 31% to 77%, but the mean score barely moved (4.28 → 4.35).
- **Reading the stories showed:** drafts too long, morals stated at Lesson 2, problems solved by luck, noisy parent notes, and a "really scary" request for a 9-year-old that lost all spookiness.

## v2: word budget, lesson rules, agency, cleaner notes

- **Changed:**
  - a code-computed word budget;
  - "no stated moral at Lesson 1–3";
  - the hero takes the decisive action;
  - at most 2 parent notes;
  - gentle spooky fun for softened scary requests.

  The baseline got the same rules, for fairness.
- **Result:** the baseline improved as much as the pipeline (pass rate 38% → 69%). The pipeline's first draft was *worse* than the baseline, because the planner's lesson field leaked into the prose, and outline-driven drafts ran long.

## v3: lesson leak, judge calibration, averaged judging

- **Changed:**
  - the outline's lesson became a planning note;
  - Energy 1 still needs an active hero;
  - the two measurement changes above.
- **Result:** the pipeline and the baseline were level on quality (4.34 vs 4.38). The pipeline was better on length (92% vs 77% in range) but worse on age fit: its adult-language outline raised the reading level.

## v4: reading level in the loop, and the judge loop on the baseline

- **Changed:**
  - a reading grade above the age band now sends the draft back for revision;
  - new eval variant: baseline + judge loop.
- **Result:**
  - Age fit recovered (3.85 → 4.00).
  - Baseline + judge loop matched or beat the full pipeline on every quality row, at about 30% lower cost.
  - Across v1–v4, the planner never added measurable quality.
- **Decision:** remove the planner.

## v5: planner removed

The pipeline became **analyzer → writer → judge loop**. The writer is the former baseline prompt. The eval now compares the writer alone with the final story after the loop.

- **Result:**
  - pass rate 15% → 62%, and length in range 54% → 92%;
  - cheaper and faster than v4's pipeline ($0.0070 vs $0.0086, 50 s vs 56 s).

## v6: writer follows the parent-facing notes

- **Found by hand:** the parent notes described a different story, because the writer no longer saw the analyzer's choices.
- **Changed:** the writer now gets those notes as `<decisions>` it must match. It gets only the notes, not the full brief, which would bring back the adult register.
- **Found in the eval:** the eval judge never received the adjustments, so identical text scored request match 4.5 and then 2.5. This was fixed in v7.

## v7: current results

Eval fix only (see the measurement table). No generation prompt changed.

| | Writer only (no judge) | With judge loop |
|---|---|---|
| Mean score | 4.35 | 4.37 |
| Mean lowest criterion | 3.81 | 3.85 |
| Pass rate | 54% | **85%** |
| Length in range | 77% | **100%** |
| Avoid-list hits | 0 | 0 |
| Cost per story | $0.0024 | $0.0059 |
| Latency | 21 s | 42 s |

**Judge consistency:**
- the two calls gave identical scores on 90% of criteria;
- they disagreed on pass/fail for 3 of 26 stories;
- the same text, scored twice, differed by 0.08 points on average.

**What the numbers say:**
- The single prompt sets overall quality, so mean scores barely move.
- The loop's job is reliability. It brings every story inside the length range and fixes the weakest criterion in drafts that fail.
- **Still open:** the two failing cases (Silliness 5, and the softened monster request) both miss on control_match. Silliness and Lesson are the hardest controls for a small model to hit exactly.
