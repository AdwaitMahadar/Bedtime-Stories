"""A scripted stand-in for the LLM, so the pipeline can be tested without network access."""

from __future__ import annotations

from collections import defaultdict, deque

from storyteller.models import (
    Character,
    CriterionScore,
    Draft,
    FollowUpIntent,
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
        category="animal",
        premise="Pip the mouse looks for his lost blanket.",
        characters=[Character(name="Pip", description="a small, brave mouse")],
        setting="a cozy burrow",
        tone="cozy and gently funny",
        must_include=["Pip"],
        favorites_to_use=[],
        sensitive_topic=None,
        adjustments=[],
        assumptions=[],
        redirect_message="Let's make a story instead!" if status == "out_of_scope" else None,
    )
    data.update(overrides)
    return StoryBrief(**data)


def draft(n_words: int = 600, word: str = "calm", title: str = "Pip and the Blanket") -> Draft:
    return Draft(title=title, text=words(n_words, word))


def verdict(*scores: int) -> JudgeVerdict:
    """Seven scores in rubric order; one value means all seven get it."""
    values = list(scores) * 7 if len(scores) == 1 else list(scores)
    names = [
        "age_fit", "emotional_safety", "story_arc", "request_match",
        "control_match", "engagement", "bedtime_ending",
    ]
    fields = {
        name: CriterionScore(score=v, evidence="\"quoted line\"" if v < 4 else "fine", fix="do X" if v < 4 else "")
        for name, v in zip(names, values)
    }
    return JudgeVerdict(**fields, summary="Strength and fix.")


def intent(kind: str = "edit", status: str = "ok", instructions: str = "Make it funnier.") -> FollowUpIntent:
    return FollowUpIntent(
        status=status,
        kind=kind,
        instructions=instructions,
        adjustments=[],
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
