"""Typed contracts passed between pipeline steps.

Two kinds of models live here:
- **LLM outputs** (StoryBrief, Draft, JudgeVerdict with Issue, FollowUpIntent): sent to the API as
  JSON schemas, so their field descriptions double as instructions to the model.
- **App state** (controls, profile, check results, rounds, versions, sessions, trace): built in code.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Category = Literal["adventure", "friendship", "funny", "animal", "fantasy", "everyday", "calm", "supportive"]
Length = Literal["short", "medium", "long"]
Status = Literal["ok", "adjusted", "out_of_scope"]

CRITERIA = (
    "age_fit",
    "emotional_safety",
    "story_arc",
    "request_match",
    "control_match",
    "engagement",
    "bedtime_ending",
)


# --- User inputs ----------------------------------------------------------------------------


class StoryControls(BaseModel):
    length: Length = "medium"
    energy: int = Field(2, ge=1, le=5, description="1 = sleepy and calm, 5 = adventurous")
    silliness: int = Field(3, ge=1, le=5, description="1 = gentle, 5 = very silly")
    lesson: int = Field(2, ge=1, le=5, description="1 = just for fun, 5 = clear moral")
    story_type: Category | None = None  # None = let the writer choose


class ChildProfile(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    age: int = Field(7, ge=5, le=10)
    pronouns: Literal["she", "he", "they", "unspecified"] = "unspecified"
    child_is_hero: bool = True
    favorites: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)


class StoryRequest(BaseModel):
    text: str
    profile: ChildProfile | None = None
    controls: StoryControls = Field(default_factory=StoryControls)

    @property
    def age(self) -> int:
        return self.profile.age if self.profile else 7

    @property
    def avoid(self) -> list[str]:
        return self.profile.avoid if self.profile else []


def tidy(text: str | None) -> str | None:
    """Trim whitespace and stray closing brackets left over from the JSON output.

    Seen in testing: a redirect message ended in "instead.}". A closing bracket is only removed when it has
    no matching opening bracket in the text, so a story that really uses brackets is left alone.
    """
    if text is None:
        return None
    text = text.strip()
    for close, open_ in (("}", "{"), ("]", "[")):
        while text.endswith(close) and text.count(close) > text.count(open_):
            text = text[:-1].rstrip()
    return text


# --- LLM outputs -----------------------------------------------------------------------------


class StoryBrief(BaseModel):
    """Request check output: whether to tell this story, and what must change first.

    It plans nothing: the writer makes every creative choice and writes the parent's notes itself,
    so the notes always describe the story that was actually written.
    """

    status: Status = Field(
        description="ok = fine as asked; adjusted = needs softening to suit ages 5-10; "
        "out_of_scope = not a story request, or impossible to make child-appropriate."
    )
    adjustments: list[str] = Field(
        description="For adjusted: each change the story must make, one short sentence each "
        "(what to soften and into what, keeping the spirit). Empty if the request is fine as asked."
    )
    sensitive_topic: str | None = Field(
        description="Set when the parent raises a real-life situation (new sibling, moving house, missing a pet...). Otherwise null."
    )
    language: str = Field(description="ISO 639-1 code of the language the request is written in, e.g. 'en' or 'es'.")
    redirect_message: str | None = Field(
        description="Only for out_of_scope: a warm one-or-two sentence reply that suggests a story idea instead. Otherwise null."
    )

    @field_validator("redirect_message")
    @classmethod
    def _tidy_redirect(cls, value: str | None) -> str | None:
        return tidy(value)


class Draft(BaseModel):
    # The story comes before the title: the model writes fields in order, and a title written first
    # used a default name that the story then didn't use ("Biscuit Counts the Clouds" for a bunny called Pippin).
    text: str = Field(
        description="The full story as plain paragraphs separated by blank lines. No headings or markdown."
    )
    title: str = Field(description="A short, warm title that fits the story above, using its characters' names.")
    notes: list[str] = Field(
        description="Only when something the parent asked for had to change: one short sentence per change, in the "
        "story's language, saying what was done instead. Empty otherwise."
    )

    @field_validator("text", "title")
    @classmethod
    def _tidy_text(cls, value: str) -> str:
        return tidy(value)

    @field_validator("notes")
    @classmethod
    def _tidy_notes(cls, value: list[str]) -> list[str]:
        return [tidy(note) for note in value if tidy(note)]


IssueKind = Literal["safety", "consistency", "missing_request", "notes_mismatch", "stated_moral", "ending", "age"]


class Issue(BaseModel):
    """A concrete problem a parent would notice. Any issue sends the draft back for revision."""

    kind: IssueKind
    quote: str = Field(description="The exact words from the story or the parent notes that show the problem.")
    fix: str = Field(
        description="A specific change that fixes it. If no change is needed, it isn't an issue: leave it out."
    )


class CriterionScore(BaseModel):
    score: int = Field(ge=1, le=5)
    note: str = Field(description="One short reason for the score.")


class JudgeVerdict(BaseModel):
    """Judge output: the must-fix issues decide pass/fail; the scores are for display and the eval."""

    issues: list[Issue] = Field(description="Every must-fix problem found. Empty if there are none.")
    age_fit: CriterionScore
    emotional_safety: CriterionScore
    story_arc: CriterionScore
    request_match: CriterionScore
    control_match: CriterionScore
    engagement: CriterionScore
    bedtime_ending: CriterionScore
    summary: str = Field(description="Two sentences: the story's biggest strength and what would most improve it.")

    def scores(self) -> dict[str, CriterionScore]:
        return {name: getattr(self, name) for name in CRITERIA}

    @property
    def min_score(self) -> int:
        return min(s.score for s in self.scores().values())

    @property
    def mean_score(self) -> float:
        values = [s.score for s in self.scores().values()]
        return sum(values) / len(values)


class FollowUpIntent(BaseModel):
    """Follow-up router output: what kind of change this is. The parent's own words go to the reviser."""

    status: Status = Field(
        description="ok = fine as asked; adjusted = the change needs softening to suit ages 5-10; "
        "out_of_scope = not about the story, or impossible to make child-appropriate."
    )
    kind: Literal["edit", "new"] = Field(
        description="edit = change the current story; new = the parent wants a different story."
    )
    adjustments: list[str] = Field(
        description="For adjusted: what to soften in the requested change, one short sentence each. Otherwise empty."
    )
    redirect_message: str | None = Field(
        description="Only for out_of_scope: a warm short reply steering back to the story. Otherwise null."
    )

    @field_validator("redirect_message")
    @classmethod
    def _tidy_redirect(cls, value: str | None) -> str | None:
        return tidy(value)


# --- Built in code ---------------------------------------------------------------------------


class CheckResults(BaseModel):
    word_count: int
    length_target: int
    length_min: int
    length_max: int
    length_ok: bool
    read_minutes: float
    reading_grade: float
    grade_min: float
    grade_max: float
    language: str = "en"  # the reading-level formula only works for English
    avoid_hits: list[str]


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(UTC)


class TraceEvent(BaseModel):
    """One model call attempt. Metadata only: never the prompt or the response text."""

    at: datetime = Field(default_factory=_now)  # when the attempt finished
    step: str
    model: str
    attempt: int
    ok: bool
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    stop_reason: str | None = None
    error: str | None = None


class RoundResult(BaseModel):
    """One judge round: the draft, its code checks, the judge's verdict and the gate decision."""

    round: int
    draft: Draft
    checks: CheckResults
    verdict: JudgeVerdict
    failures: list[str]  # problems that block "passed": issues, low scores, avoid-list, unchanged follow-up
    soft_failures: list[str] = Field(default_factory=list)  # length or reading level: revised, but don't block

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def clean(self) -> bool:
        """Nothing at all left to fix."""
        return not self.failures and not self.soft_failures


class StoryVersion(BaseModel):
    number: int
    origin: str  # "initial request", or what the parent typed / clicked
    instruction: str | None = None  # the revision instruction this version applied
    controls: StoryControls
    adjustments: list[str] = Field(default_factory=list)  # softening the follow-up router asked for
    rounds: list[RoundResult]
    best_round: int  # index into rounds
    stopped_early: str | None = None  # set when the per-story budget ended the judge loop

    @property
    def best(self) -> RoundResult:
        return self.rounds[self.best_round]


class StorySession(BaseModel):
    id: str = Field(default_factory=_new_id)
    created_at: datetime = Field(default_factory=_now)
    request: StoryRequest
    brief: StoryBrief
    versions: list[StoryVersion] = Field(default_factory=list)
    trace: list[TraceEvent] = Field(default_factory=list)

    @property
    def current(self) -> StoryVersion | None:
        return self.versions[-1] if self.versions else None

    @property
    def total_cost(self) -> float:
        return sum(e.cost_usd for e in self.trace)


class FollowUpOutcome(BaseModel):
    """What a follow-up produced: a new version, a new story, or a redirect message."""

    kind: Literal["edit", "new", "redirect"]
    session: StorySession  # the session that now holds the result (a new one for "new")
    message: str | None = None
