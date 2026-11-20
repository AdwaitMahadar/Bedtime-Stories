"""Typed contracts passed between pipeline steps.

Two kinds of models live here:
- **LLM outputs** (StoryBrief, Draft, JudgeVerdict, FollowUpIntent): sent to the API as
  JSON schemas, so their field descriptions double as instructions to the model.
- **App state** (controls, profile, check results, rounds, versions, sessions, trace): built in code.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

Category = Literal[
    "adventure", "friendship", "funny", "animal", "fantasy", "everyday", "calm", "supportive"
]
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
    story_type: Optional[Category] = None  # None = let the analyzer choose


class ChildProfile(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    age: int = Field(7, ge=5, le=10)
    pronouns: Literal["she", "he", "they", "unspecified"] = "unspecified"
    child_is_hero: bool = True
    favorites: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)


class StoryRequest(BaseModel):
    text: str
    profile: Optional[ChildProfile] = None
    controls: StoryControls = Field(default_factory=StoryControls)

    @property
    def age(self) -> int:
        return self.profile.age if self.profile else 7

    @property
    def avoid(self) -> list[str]:
        return self.profile.avoid if self.profile else []


# --- LLM outputs -----------------------------------------------------------------------------


class Character(BaseModel):
    name: str
    description: str = Field(description="One short line: who they are and one memorable trait.")


class StoryBrief(BaseModel):
    """Analyzer output: what story to tell, and whether the request needed changes."""

    status: Status = Field(
        description="ok = fine as asked; adjusted = softened or changed to suit ages 5-10; "
        "out_of_scope = not a story request, or impossible to make child-appropriate."
    )
    category: Category
    premise: str = Field(description="One or two sentences: who the story is about and what happens.")
    characters: list[Character]
    setting: str
    tone: str = Field(description="A few words, e.g. 'cozy and gently funny'.")
    must_include: list[str] = Field(
        description="Concrete elements from the request that the story must contain (names, animals, places, objects)."
    )
    favorites_to_use: list[str] = Field(
        description="0-2 of the child's favorite things that fit this story naturally. Empty if none fit."
    )
    sensitive_topic: Optional[str] = Field(
        description="Set when the parent raises a real-life topic (new sibling, moving house, missing a pet...). Otherwise null."
    )
    adjustments: list[str] = Field(
        description="Parent-facing notes, one friendly sentence each, explaining anything changed from the request. Empty if nothing changed."
    )
    assumptions: list[str] = Field(
        description="Parent-facing notes on choices made because the request didn't specify them. Empty if the request was specific."
    )
    redirect_message: Optional[str] = Field(
        description="Only for out_of_scope: a warm one-or-two sentence reply that suggests a story idea instead. Otherwise null."
    )


class Draft(BaseModel):
    title: str
    text: str = Field(description="The full story as plain paragraphs separated by blank lines. No headings or markdown.")


class CriterionScore(BaseModel):
    score: int = Field(ge=1, le=5)
    evidence: str = Field(
        description="For scores of 3 or less: an exact quote of the line at fault. Otherwise a short reason, or empty."
    )
    fix: str = Field(description="For scores of 3 or less: a specific, actionable fix. Otherwise empty.")


class JudgeVerdict(BaseModel):
    """Judge output. One named field per criterion, so the schema guarantees none is skipped."""

    age_fit: CriterionScore
    emotional_safety: CriterionScore
    story_arc: CriterionScore
    request_match: CriterionScore
    control_match: CriterionScore
    engagement: CriterionScore
    bedtime_ending: CriterionScore
    summary: str = Field(description="Two sentences: the story's biggest strength and its most important fix.")

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
    """Follow-up router output: what the parent's change request means."""

    status: Status = Field(
        description="ok = fine as asked; adjusted = the change was softened to suit ages 5-10; "
        "out_of_scope = not about the story, or impossible to make child-appropriate."
    )
    kind: Literal["edit", "new"] = Field(
        description="edit = change the current story; new = the parent wants a different story."
    )
    instructions: str = Field(
        description="For edit: a clear instruction for the reviser. For new: the new story request, written as a request."
    )
    adjustments: list[str] = Field(
        description="Parent-facing notes, one friendly sentence each, on anything softened. Empty if nothing changed."
    )
    redirect_message: Optional[str] = Field(
        description="Only for out_of_scope: a warm short reply steering back to the story. Otherwise null."
    )


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
    grade_ok: bool  # within the age band; a grade above it sends the draft back for simplifying
    avoid_hits: list[str]


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
    stop_reason: Optional[str] = None
    error: Optional[str] = None


class RoundResult(BaseModel):
    """One judge round: the draft, its code checks, the judge's verdict and the gate decision."""

    round: int
    draft: Draft
    checks: CheckResults
    verdict: JudgeVerdict
    failures: list[str]  # why the gate failed; empty = passed

    @property
    def passed(self) -> bool:
        return not self.failures


class StoryVersion(BaseModel):
    number: int
    origin: str  # "initial request", or what the parent typed / clicked
    instruction: Optional[str] = None  # the revision instruction this version applied
    controls: StoryControls
    notes: list[str] = Field(default_factory=list)  # adjustment notes shown to the parent
    rounds: list[RoundResult]
    best_round: int  # index into rounds
    stopped_early: Optional[str] = None  # set when the per-story budget ended the judge loop

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
    def current(self) -> Optional[StoryVersion]:
        return self.versions[-1] if self.versions else None

    @property
    def total_cost(self) -> float:
        return sum(e.cost_usd for e in self.trace)


class FollowUpOutcome(BaseModel):
    """What a follow-up produced: a new version, a new story, or a redirect message."""

    kind: Literal["edit", "new", "redirect"]
    session: StorySession  # the session that now holds the result (a new one for "new")
    message: Optional[str] = None
