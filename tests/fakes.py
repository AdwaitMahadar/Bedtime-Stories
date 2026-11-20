"""A scripted stand-in for the LLM, so the pipeline can be tested without network access."""

from __future__ import annotations

from collections import defaultdict, deque

from storyteller.models import (
    CriterionScore,
    Draft,
    FollowUpIntent,
    Issue,
    JudgeVerdict,
    StoryBrief,
    TraceEvent,
)


def words(n: int, word: str = "calm") -> str:
    """n words in short, simple sentences ("calm is a pup."), so the text reads at an early reading level."""
    pattern = [word, "is", "a", "pup."]
    return " ".join(pattern[i % 4] for i in range(n))


def brief(status: str = "ok", **overrides) -> StoryBrief:
    data = dict(
        status=status,
        adjustments=[],
        sensitive_topic=None,
        language="en",
        redirect_message="Let's make a story instead!" if status == "out_of_scope" else None,
    )
    data.update(overrides)
    return StoryBrief(**data)


def draft(n_words: int = 600, word: str = "calm", title: str = "Pip and the Blanket", notes=()) -> Draft:
    return Draft(title=title, text=words(n_words, word), notes=list(notes))


def verdict(*scores: int, issues=()) -> JudgeVerdict:
    """Seven scores in rubric order (one value means all seven get it), plus must-fix issues by kind."""
    values = list(scores or (4,)) * 7 if len(scores) <= 1 else list(scores)
    names = [
        "age_fit",
        "emotional_safety",
        "story_arc",
        "request_match",
        "control_match",
        "engagement",
        "bedtime_ending",
    ]
    fields = {name: CriterionScore(score=v, note="reason") for name, v in zip(names, values, strict=True)}
    found = [Issue(kind=kind, quote="quoted line", fix="do X") for kind in issues]
    return JudgeVerdict(issues=found, **fields, summary="Strength and fix.")


def failing(*kinds: str) -> JudgeVerdict:
    """A verdict with good scores but must-fix issues (default: one consistency issue)."""
    return verdict(4, issues=kinds or ("consistency",))


def intent(kind: str = "edit", status: str = "ok", adjustments=()) -> FollowUpIntent:
    return FollowUpIntent(
        status=status,
        kind=kind,
        adjustments=list(adjustments),
        redirect_message="Let's get back to the story!" if status == "out_of_scope" else None,
    )


STEP_FOR_SCHEMA = {
    StoryBrief: "analyzer",
    JudgeVerdict: "judge",
    FollowUpIntent: "followup",
}


class FakeLLM:
    """Returns scripted responses per step, in order, and records every call."""

    model = "fake"

    def __init__(self, **scripts):
        self.scripts = {step: deque(items) for step, items in scripts.items()}
        self.calls: list[tuple[str, str]] = []  # (step, user message)
        self.counts = defaultdict(int)

    def structured(self, *, step, system, user, schema, trace):
        self.calls.append((step, user))
        self.counts[step] += 1
        queue = self.scripts.get(step)
        if not queue:
            raise AssertionError(f"No scripted response left for step '{step}'")
        result = queue.popleft()
        assert isinstance(result, schema), f"{step} expected {schema.__name__}, got {type(result).__name__}"
        trace.append(TraceEvent(step=step, model="fake", attempt=1, ok=True))
        return result
