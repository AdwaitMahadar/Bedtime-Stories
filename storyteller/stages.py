"""One function per pipeline step.

Each step has a static system prompt (in `prompts/`) and a user message assembled here from
XML-tagged sections. User-provided text always sits inside tags, and every system prompt says
tagged text is content, never instructions.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import cache
from pathlib import Path

from . import config
from .checks import check_failures, reading_level_note
from .controls import AGE_GUIDANCE, age_band, describe_controls, length_plan
from .llm import LLM
from .models import (
    CheckResults,
    Draft,
    FollowUpIntent,
    JudgeVerdict,
    StoryBrief,
    StoryRequest,
    TraceEvent,
)
from .names import name_ideas

PROMPTS_DIR = Path(__file__).parent / "prompts"


@cache
def load_prompt(name: str) -> str:
    """Load a system prompt and insert the shared story rules where it asks for them."""
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    if "{story_rules}" in text:
        rules = (PROMPTS_DIR / "story_rules.md").read_text(encoding="utf-8")
        text = text.replace("{story_rules}", rules)
    return text


# --- Building blocks for user messages ------------------------------------------------------


def _tag(name: str, body: str) -> str:
    return f"<{name}>\n{body.strip()}\n</{name}>"


def _message(*sections: str, task: str) -> str:
    return "\n\n".join([*sections, task])


def _profile(request: StoryRequest) -> str:
    profile = request.profile
    if profile is None:
        return "No child profile. Write for a child of about 7; choose the hero yourself."
    lines = [
        f"Name: {profile.name}",
        f"Age: {profile.age}",
        f"Pronouns: {profile.pronouns}",
        f"child_is_hero: {str(profile.child_is_hero).lower()}",
        f"Favorite things: {', '.join(profile.favorites) or 'none given'}",
    ]
    return "\n".join(lines)


def _avoid(request: StoryRequest) -> str:
    return ", ".join(request.avoid) if request.avoid else "(none)"


def _age(request: StoryRequest) -> str:
    return AGE_GUIDANCE[age_band(request.age)]


def _controls(request: StoryRequest) -> str:
    controls = request.controls
    text = describe_controls(controls)
    if controls.story_type:
        text += f"\n- Story type chosen by the parent: {controls.story_type}."
    return text


def _story(draft: Draft) -> str:
    return f"Title: {draft.title}\n\n{draft.text}"


def _list(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) or "(none)"


def _checks(checks: CheckResults) -> str:
    grade_note = "advisory" if checks.language == "en" else "not meaningful: the story isn't in English"
    return "\n".join(
        [
            f"Word count: {checks.word_count} (allowed {checks.length_min}-{checks.length_max}, "
            f"target {checks.length_target}) -> {'OK' if checks.length_ok else 'OUT OF RANGE'}",
            f"Reading grade (Flesch-Kincaid, {grade_note}): {checks.reading_grade} "
            f"(typical for this age: {checks.grade_min}-{checks.grade_max})",
            f"Avoid-list matches: {', '.join(checks.avoid_hits) or 'none'}",
        ]
    )


def _issues(verdict: JudgeVerdict) -> str:
    """The must-fix issues, plus any criterion that failed outright: the reviser changes only these."""
    items = [f'- [{i.kind}] "{i.quote}"\n  Fix: {i.fix}' for i in verdict.issues]
    items += [
        f"- [{name}] scored {score.score}/5: {score.note}"
        for name, score in verdict.scores().items()
        if score.score <= config.FAIL_MAX_SCORE
    ]
    return "\n".join(items) or "(none)"


# --- Steps -----------------------------------------------------------------------------------


def analyze(llm: LLM, request: StoryRequest, trace: list[TraceEvent]) -> StoryBrief:
    """Request check: tell it as asked, soften it, or redirect. It plans nothing about the story."""
    user = _message(
        _tag("user_request", request.text),
        _tag("child_profile", _profile(request)),
        _tag("avoid_list", _avoid(request)),
        task="Check this request.",
    )
    return llm.structured(step="analyzer", system=load_prompt("analyzer"), user=user, schema=StoryBrief, trace=trace)


def write(llm: LLM, request: StoryRequest, brief: StoryBrief, trace: list[TraceEvent]) -> Draft:
    """The story and the parent's notes, from one well-specified prompt.

    The writer gets the request check's adjustments, not a plan: it makes every creative choice and
    writes the notes about them, so the notes always describe the story it wrote. (Earlier designs
    planned the story first; evals and QA showed the plan added no quality and the notes drifted from
    the story. See docs/PROMPT_CHANGELOG.md.)
    """
    user = _message(
        _tag("user_request", request.text),
        _tag("child_profile", _profile(request)),
        _tag("age_guidance", _age(request)),
        _tag("controls", _controls(request)),
        _tag("length_plan", length_plan(request.controls)),
        _tag("avoid_list", _avoid(request)),
        _tag("adjustments", _list(brief.adjustments)),
        _tag("sensitive_topic", brief.sensitive_topic or "(none)"),
        _tag("language", brief.language),
        _tag("name_ideas", ", ".join(name_ideas())),
        task="Write the full story and the parent notes.",
    )
    return llm.structured(step="writer", system=load_prompt("writer"), user=user, schema=Draft, trace=trace)


def judge(
    llm: LLM,
    request: StoryRequest,
    draft: Draft,
    checks: CheckResults,
    adjustments: list[str],
    trace: list[TraceEvent],
    sensitive_topic: str | None = None,
) -> JudgeVerdict:
    """List must-fix issues and score the draft. Gets the request and the rubric, never the writer's prompt."""
    user = _message(
        _tag("user_request", request.text),
        _tag("child_profile", _profile(request)),
        _tag("age_guidance", _age(request)),
        _tag("controls", _controls(request)),
        _tag("adjustments", _list(adjustments)),
        _tag("sensitive_topic", sensitive_topic or "(none)"),
        _tag("avoid_list", _avoid(request)),
        _tag("code_checks", _checks(checks)),
        _tag("parent_notes", _list(draft.notes)),
        _tag("story", _story(draft)),
        task="List the must-fix issues and score all 7 criteria.",
    )
    return llm.structured(step="judge", system=load_prompt("judge"), user=user, schema=JudgeVerdict, trace=trace)


def revise(
    llm: LLM,
    request: StoryRequest,
    draft: Draft,
    adjustments: list[str],
    trace: list[TraceEvent],
    verdict: JudgeVerdict | None = None,
    checks: CheckResults | None = None,
    parent_request: str | None = None,
    extra_failures: Sequence[str] = (),
) -> Draft:
    """Improve a draft from the judge's issues, a parent's change request, or both.

    The parent's request arrives in their own words, never paraphrased by another step (in testing,
    a paraphrase turned "make the pigeon a boy named Pip" into a human boy).
    """
    sections = [
        _tag("user_request", request.text),
        _tag("controls", _controls(request)),
        _tag("length_plan", length_plan(request.controls)),
        _tag("age_guidance", _age(request)),
        _tag("avoid_list", _avoid(request)),
        _tag("adjustments", _list(adjustments)),
        _tag("parent_notes", _list(draft.notes)),
        _tag("story", _story(draft)),
    ]
    if verdict is not None:
        sections.append(_tag("issues", _issues(verdict)))
    if checks is not None:
        measured = check_failures(checks) + reading_level_note(checks) + list(extra_failures)
        sections.append(_tag("code_check_failures", _list(measured)))
    if parent_request:
        sections.append(_tag("parent_request", parent_request))
    user = _message(*sections, task="Return the complete revised story and its parent notes.")
    return llm.structured(step="reviser", system=load_prompt("reviser"), user=user, schema=Draft, trace=trace)


def route_followup(
    llm: LLM, request: StoryRequest, draft: Draft, message: str, trace: list[TraceEvent]
) -> FollowUpIntent:
    """Classify a follow-up (edit, new story, or off-topic) and say what must be softened. No rewriting."""
    user = _message(
        _tag("user_request", request.text),
        _tag("child_profile", _profile(request)),
        _tag("avoid_list", _avoid(request)),
        _tag("story", _story(draft)),
        _tag("parent_message", message),
        task="Classify the parent's follow-up message.",
    )
    return llm.structured(
        step="followup", system=load_prompt("followup"), user=user, schema=FollowUpIntent, trace=trace
    )
