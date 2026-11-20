# Prompt changelog

Each revision came from reading eval stories and was measured with `python -m eval.run_eval`. Scores are on a 1–5 scale, averaged over 13 scored cases (the off-topic case is redirected, not scored). Every story is re-scored by fresh judge calls. Full reports and stories are in `eval/results/v1` … `v9`.

## How to read the numbers across versions

The way stories are measured changed five times. Each change made the eval more accurate, so **scores from different versions are not directly comparable**. Compare variants within a version, not a number from v1 against v7.

| Version | Measurement change | Effect |
|---|---|---|
| v3 | Judge scores request match on the request's spirit *within bedtime-safe limits* (softening is never penalized) | request_match from v3 on is not comparable with v1–v2 |
| v3 | Each story gets 2 judge calls, averaged per criterion | Stricter (a 3 from either call fails) and less noisy |
| v7 | The eval judge gets the analyzer's adjustments, as the in-app judge does | Removes noise on softened requests |
| v8 | The judge lists must-fix problems; a story passes with none (and no score of 2 or less). Length tolerance ±15 % → ±25 % | Headline number is problems per story; pass rates are not comparable with v1–v7 |
| v9 | The judge also checks secrets kept from parents, death described as sleep, garbled sentences and titles naming someone not in the story, and is told the real-life topic | Stricter: v9 problem counts are not comparable with v8 |

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

## v7: eval judge gets the adjustments

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

## v8: a judge that looks for problems

**What started it.** Reading every story from 18 realistic requests with 10 follow-ups (`python -m eval.qa_run`, not just the eval averages) showed that the judge spent its rounds on matters of taste and missed what a parent would notice:
- **A paraphrase changed the meaning.** "Make the pigeon a boy named Pip" became a human boy who climbs a lamppost and lands on a horse's head. The judge passed it.
- **Notes described a story that wasn't written.** The analyzer chose characters and wrote the parent's notes before the writer ran, and the writer never saw that plan. One note said the pigeon helps the horse "find her way home"; the story was about a lost ribbon.
- **An avoid-list request was half-handled.** For "add a big hairy spider" (spiders on the avoid-list), the code check swapped in a bumblebee, the note still said "I made the spider gentle", and the judge marked it down three rounds running.
- **The judge fought deliberate softening.** On "really scary" it asked for more fear in all 6 rounds; the reviser added a tumble down the stairs.
- **It missed real problems:** a child sneaking outside alone at night, a pronoun changing mid-story, a title unrelated to the story.
- **Code checks burned rounds on noise:** a grief story 37 words short, a reading grade of 2.9 against 2.5, a Spanish story graded by an English formula. 9 of 26 versions ended "best effort"; a third round passed only 3 times in 12.

**Changes:**
- **Judge:** a must-fix checklist (safety including behavior a child might copy, consistency, missing request, notes mismatch, stated moral, ending, age), each issue quoted with a fix. The 7 scores stay for display and the eval, and only fail a draft at 2 or below.
- **Request check instead of a brief:** the analyzer only decides ok / adjusted / out of scope, what to soften (what characters do, never who they are), the language and any real-life topic. The writer makes every creative choice and writes the notes.
- **Parent's words go to the reviser unchanged;** the follow-up router only classifies. The avoid-list is checked against follow-ups in code.
- **Notes only when something was changed,** enforced in code.
- **Code rules:** length ±25 %, reading level only when more than 1 grade too hard and only for English, at most 2 reviews.
- **Story rules:** keep every name the parent gives; characters act like what they are; spell out unsafe behavior a child might copy (cooking at the stove, leaving the house at night).

**What went wrong while tuning:**
- Banning overused names ("not Pip or Pim") made the writer correct itself mid-story: "Pim, no, not Pim." The ban was removed.
- "Invent uncommon names" then renamed a family's real dog in a grief story. Parent-given names are now always kept.
- A paragraph-count hint made first drafts shorter (75 % of the target instead of 88 %), so it was removed.
- Some new prompt examples matched the test cases (an owl crossing a stream, blood, spiders), which would have flattered the results. They were replaced with unrelated examples.

Full reports: [eval/results/v8/report.md](../eval/results/v8/report.md) and [eval/results/v8-repeat/report.md](../eval/results/v8-repeat/report.md), with identical prompts.

| | Writer only (no judge) | With judge loop |
|---|---|---|
| Must-fix problems per story (run 1 / run 2) | 0.42 / 0.62 | **0.04 / 0.27** |
| Pass rate (run 1 / run 2) | 58% / 46% | **96% / 73%** |
| Mean score | 4.29 / 4.31 | 4.37 / 4.37 |
| Length in range | 85% / 69% | 100% / 100% |
| Avoid-list hits | 0 | 0 |
| Cost per story | $0.0012 | $0.0036 / $0.0039 |
| Latency | 11 s / 10 s | 25 s / 26 s |

**What the numbers say:**
- **The two runs differ a lot with identical prompts,** so read the average: the loop cuts must-fix problems from about 0.5 to about 0.15 per story, and the pass rate rises from about 52 % to about 85 %.
- **Faster and cheaper than v7** (26 s and $0.004 per story, from 42 s and $0.006), because the request check is shorter, the writer thinks less, and stories stop after 2 reviews.
- **What remains:** stated morals (11 of 16 first-draft issues in run 2) are still the most common problem, as since v1. The app's single judge call misses some problems the eval's two calls find.

## v9: fixes from reading every story and draft (current results)

**What started it.** After v8 we read every story again, including every intermediate draft and follow-up version (18 requests, 10 follow-ups, plus the eval stories), instead of relying on the judge's lists. That found:
- **A follow-up that did nothing.** "Make it scarier, with blood" came back word for word the same; only the note changed, and it was marked "Passed review". The judge never sees the previous version.
- **Titles naming characters who aren't in the story** ("Biscuit Counts the Clouds" for a bunny called Pippin). The title was written before the story, using the model's default name.
- **The judge's own fixes creating contradictions,** applied word for word: a button that "rolled out of the jar last week" although it went missing today.
- **Secrets from grown-ups** passed as fine ("Quietly, so Mum and Dad don't wake up"; "I won't tell").
- **"The child isn't the hero" ignored,** and **death framed as sleep** at the end of a grief story ("Goodnight, Biscuit… Sleep now").
- **"Best effort" on good stories** that were only a few words short, and the same handful of names in story after story (Pip, Pim, Biscuit).

**Changes:**
- Code: a follow-up whose story comes back unchanged is sent back with the parent's words; the story is written before the title (field order); length and reading level still get a revision but no longer make a story "best effort"; the writer gets 5 random name ideas from a list of uncommon names; the judge is told the real-life topic.
- Prompts: fixes are hints that must fit the whole story; no secrets from parents; a line for when the child isn't the hero; say "died", never sleep or going away.
- **Over-correction, then recalibration.** The broader safety wording made the judge flag ordinary independence (a girl riding in a cart, a cat hopping off a sofa, a dragon breathing a candle-sized flame), and its fixes handed the child's solution to a grown-up and added lectures ("Grown-ups first, and nobody runs ahead"). The rules now cover only what is genuinely dangerous if copied in real life, say that ordinary independence is good and creatures do what they do, and ask fixes to keep the child's own solution. The judge lists an issue only when a change is needed.
- **Up to 3 reviews again.** With concrete must-fix issues, a second review sometimes caught a real problem ("Please don't tell the grown-ups") with no round left to fix it. A third round runs only in that case; average time is unchanged.

Full reports: [v9-3rounds](../eval/results/v9-3rounds/report.md) and [v9-3rounds-repeat](../eval/results/v9-3rounds-repeat/report.md) (identical prompts, the shipped settings). The 2-round runs [v9](../eval/results/v9/report.md) and [v9-repeat](../eval/results/v9-repeat/report.md) are kept for comparison.

| | Writer only (no judge) | With judge loop |
|---|---|---|
| Must-fix problems per story (run 1 / run 2) | 0.42 / 0.42 | **0.15 / 0.27** |
| Pass rate (run 1 / run 2) | 62% / 50% | **85% / 73%** |
| Mean score | 4.41 / 4.31 | 4.39 / 4.38 |
| Length in range | 85% / 77% | 100% / 100% |
| Avoid-list hits | 0 | 0 |
| Cost per story | $0.0021 / $0.0016 | $0.0047 / $0.0052 |
| Latency | 19 s / 14 s | 33 s / 35 s |

**What the numbers say:**
- The loop roughly halves the must-fix problems (about 0.42 to about 0.21 per story) and lifts the pass rate from about 56 % to about 79 %, against a stricter checklist than v8's.
- The two 2-round runs ended at 0.46 and 0.38 problems per story, but their first drafts also differed a lot (0.85 and 0.54), so the third round's benefit can't be separated from run-to-run noise. It's kept because it costs nothing unless a real problem is left.
- **What remains:** the judge is still inconsistent from call to call (the same line can be flagged in one review and not the next), and stated morals and occasional over-cautious safety flags are the most common issues.

