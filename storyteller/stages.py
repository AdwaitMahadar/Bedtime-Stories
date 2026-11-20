"""One function per pipeline step.

Each step has a static system prompt (in `prompts/`) and a user message assembled here from
XML-tagged sections. User-provided text always sits inside tags, and every system prompt says
tagged text is content, never instructions.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

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

PROMPTS_DIR = Path(__file__).parent / "prompts"


@lru_cache(maxsize=None)
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


def _brief(brief: StoryBrief) -> str:
    return brief.model_dump_json(indent=2, exclude={"status", "redirect_message", "assumptions"})


def _decisions(brief: StoryBrief) -> str:
    notes = brief.adjustments + brief.assumptions
    return "\n".join(f"- {n}" for n in notes) or "(none)"


def _checks(checks: CheckResults) -> str:
    return "\n".join(
        [
            f"Word count: {checks.word_count} (allowed {checks.length_min}-{checks.length_max}, "
            f"target {checks.length_target}) -> {'OK' if checks.length_ok else 'OUT OF RANGE'}",
            f"Reading grade (Flesch-Kincaid, advisory): {checks.reading_grade} "
            f"(typical for this age: {checks.grade_min}-{checks.grade_max})",
            f"Avoid-list matches: {', '.join(checks.avoid_hits) or 'none'}",
        ]
    )


def _feedback(verdict: JudgeVerdict) -> str:
    """Only the criteria below the pass bar: the reviser should change what's flagged and nothing else."""
    items = []
    for name, score in verdict.scores().items():
        if score.score < config.PASS_MIN_SCORE:
            items.append(
                f"- {name} scored {score.score}/5\n  Problem: {score.evidence}\n  Fix: {score.fix}"
            )
    return "\n".join(items) or "No criteria below the bar."


# --- Steps -----------------------------------------------------------------------------------


def analyze(llm: LLM, request: StoryRequest, trace: list[TraceEvent]) -> StoryBrief:
    user = _message(
        _tag("user_request", request.text),
        _tag("child_profile", _profile(request)),
        _tag("avoid_list", _avoid(request)),
        _tag("controls", _controls(request)),
        task="Write the story brief for this request.",
    )
    return llm.structured(
        step="analyzer", system=load_prompt("analyzer"), user=user, schema=StoryBrief, trace=trace
    )


def write(llm: LLM, request: StoryRequest, brief: StoryBrief, trace: list[TraceEvent]) -> Draft:
    """First draft from one well-specified prompt: the request, profile, age, controls and word budget.

    Of the brief, the writer gets only the parent-facing choices (adjustments and assumptions), so
    the story matches the notes the parent sees. The full brief is left out: its adult planning
    language pulled the writing above the child's reading level (eval v3).

    An earlier version planned an outline first (planner -> writer). Evals showed no quality gain
    over this single prompt, so the planner was removed; see docs/PROMPT_CHANGELOG.md (v4).
    """
    user = _message(
        _tag("user_request", request.text),
        _tag("child_profile", _profile(request)),
        _tag("age_guidance", _age(request)),
        _tag("controls", _controls(request)),
        _tag("length_plan", length_plan(request.controls)),
        _tag("avoid_list", _avoid(request)),
        _tag("decisions", _decisions(brief)),
        task="Write the full story.",
    )
    return llm.structured(step="writer", system=load_prompt("writer"), user=user, schema=Draft, trace=trace)


def judge(
    llm: LLM,
    request: StoryRequest,
    draft: Draft,
    checks: CheckResults,
    adjustments: list[str],
    trace: list[TraceEvent],
) -> JudgeVerdict:
    """Score a draft. Gets the original request and the rubric, never the writer's prompt."""
    user = _message(
        _tag("user_request", request.text),
        _tag("child_profile", _profile(request)),
        _tag("age_guidance", _age(request)),
        _tag("controls", _controls(request)),
        _tag("adjustments", "\n".join(f"- {a}" for a in adjustments) or "(none)"),
        _tag("avoid_list", _avoid(request)),
        _tag("code_checks", _checks(checks)),
        _tag("story", _story(draft)),
        task="Review the story and score all 7 criteria.",
    )
    return llm.structured(step="judge", system=load_prompt("judge"), user=user, schema=JudgeVerdict, trace=trace)


def revise(
    llm: LLM,
    request: StoryRequest,
    brief: StoryBrief,
    draft: Draft,
    trace: list[TraceEvent],
    verdict: Optional[JudgeVerdict] = None,
    checks: Optional[CheckResults] = None,
    instruction: Optional[str] = None,
) -> Draft:
    """Improve a draft from judge feedback, a parent's instruction, or both."""
    sections = [
        _tag("user_request", request.text),
        _tag("brief", _brief(brief)),
        _tag("controls", _controls(request)),
        _tag("length_plan", length_plan(request.controls)),
        _tag("age_guidance", _age(request)),
        _tag("avoid_list", _avoid(request)),
        _tag("story", _story(draft)),
    ]
    if verdict is not None:
        sections.append(_tag("judge_feedback", _feedback(verdict)))
    if checks is not None:
        measured = check_failures(checks) + reading_level_note(checks)
        sections.append(_tag("code_check_failures", "\n".join(measured) or "(none)"))
    if instruction:
        sections.append(_tag("parent_instruction", instruction))
    user = _message(*sections, task="Return the complete revised story.")
    return llm.structured(step="reviser", system=load_prompt("reviser"), user=user, schema=Draft, trace=trace)


def route_followup(
    llm: LLM, request: StoryRequest, draft: Draft, message: str, trace: list[TraceEvent]
) -> FollowUpIntent:
    user = _message(
        _tag("user_request", request.text),
        _tag("child_profile", _profile(request)),
        _tag("avoid_list", _avoid(request)),
        _tag("story", _story(draft)),
        _tag("parent_message", message),
        task="Route the parent's follow-up message.",
    )
    return llm.structured(
        step="followup", system=load_prompt("followup"), user=user, schema=FollowUpIntent, trace=trace
    )
