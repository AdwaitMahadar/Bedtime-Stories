"""Code checks: what can be measured exactly is measured in code, not by the LLM.

- Length: word count vs the target (affects pass/fail).
- Reading level: Flesch-Kincaid grade vs the age band. Context for the judge; a grade above the band
  sends the draft back for one simplifying revision (`reading_level_note`), but isn't a quality score.
- Avoid-list: whole-word matches of the child's avoid terms (any hit fails the draft).
"""

from __future__ import annotations

import re

import textstat

from . import config
from .controls import GRADE_RANGE, age_band, length_bounds, length_target
from .models import CheckResults, Draft, StoryControls

_WORD = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*")


def count_words(text: str) -> int:
    return len(_WORD.findall(text))


def _variants(term: str) -> set[str]:
    """Singular and simple plural forms of a term ("spider" <-> "spiders", "box" <-> "boxes")."""
    forms = {term, term + "s", term + "es"}
    if term.endswith("es") and len(term) > 4:
        forms.add(term[:-2])
    if term.endswith("s") and len(term) > 3:
        forms.add(term[:-1])
    return forms


def _term_pattern(term: str) -> re.Pattern[str]:
    """Match any form of the term as whole words, case-insensitively; spaces match any whitespace."""
    forms = sorted(_variants(" ".join(term.lower().split())), key=len, reverse=True)
    body = "|".join(re.escape(form).replace(r"\ ", r"\s+") for form in forms)
    return re.compile(rf"\b(?:{body})\b", re.IGNORECASE)


def find_avoid_hits(text: str, avoid: list[str]) -> list[str]:
    """Return the avoid-list terms that appear in the text."""
    hits = []
    for term in avoid:
        term = term.strip()
        if term and _term_pattern(term).search(text):
            hits.append(term)
    return hits


def run_checks(draft: Draft, controls: StoryControls, age: int, avoid: list[str]) -> CheckResults:
    text = f"{draft.title}\n\n{draft.text}"
    words = count_words(draft.text)
    low, high = length_bounds(controls)
    grade = round(float(textstat.flesch_kincaid_grade(draft.text)), 1)
    grade_min, grade_max = GRADE_RANGE[age_band(age)]
    return CheckResults(
        word_count=words,
        length_target=length_target(controls),
        length_min=low,
        length_max=high,
        length_ok=low <= words <= high,
        read_minutes=round(words / config.READ_ALOUD_WPM, 1),
        reading_grade=grade,
        grade_min=grade_min,
        grade_max=grade_max,
        grade_ok=grade_min <= grade <= grade_max,
        avoid_hits=find_avoid_hits(text, avoid),
    )


def reading_level_note(checks: CheckResults) -> list[str]:
    """A revision note when the draft reads above the child's age band (empty otherwise).

    The formula is noisy, so this only triggers a simplifying revision inside the pipeline; it is not
    part of `check_failures`, which defines pass/fail for the eval.
    """
    if checks.reading_grade <= checks.grade_max:
        return []
    return [
        f"Reading level too high: grade {checks.reading_grade}, above {checks.grade_max} for this age. "
        "Split long sentences, swap harder words and comparisons for ones from the child's everyday world, "
        "and keep the plot, the voice and the calm ending."
    ]


def check_failures(checks: CheckResults) -> list[str]:
    """Measured problems that fail a draft, phrased as instructions the reviser can act on."""
    failures = []
    if checks.word_count < checks.length_min:
        failures.append(
            f"Too short: {checks.word_count} words. Lengthen to {checks.length_min}-{checks.length_max} "
            f"(target {checks.length_target}) by deepening existing moments."
        )
    elif checks.word_count > checks.length_max:
        failures.append(
            f"Too long: {checks.word_count} words. Shorten to {checks.length_min}-{checks.length_max} "
            f"(target {checks.length_target}) by tightening, keeping every beat and the calm ending."
        )
    for term in checks.avoid_hits:
        failures.append(f"Avoid-list item found: '{term}'. Remove every trace of it, including synonyms.")
    return failures
