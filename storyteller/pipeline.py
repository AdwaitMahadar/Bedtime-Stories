"""Orchestration: analyzer -> writer -> [code checks -> judge -> reviser]* -> best draft.

The pass/fail decision (`gate`) and the choice of the best draft (`pick_best`) are plain functions,
so the rules are explicit and unit-tested rather than hidden in a prompt. Each run (a new story or a
follow-up) is capped by a per-story `Budget` of calls, cost and time.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from . import config, stages
from .budget import Budget, BudgetTracker
from .checks import check_failures, reading_level_note, run_checks
from .controls import apply_quick_action, describe_control_changes
from .llm import LLM
from .models import (
    Draft,
    FollowUpOutcome,
    JudgeVerdict,
    RoundResult,
    StoryBrief,
    StoryControls,
    StoryRequest,
    StorySession,
    StoryVersion,
    TraceEvent,
)

ProgressFn = Callable[[str, str], None]  # (step, human-readable detail)


# --- Rules -----------------------------------------------------------------------------------


def gate(verdict: JudgeVerdict, checks) -> list[str]:
    """Why a draft fails, or an empty list if it passes.

    Passes when every criterion scores >= PASS_MIN_SCORE, the length is in range, nothing from the
    avoid-list appears, and the reading grade isn't above the child's age band.
    """
    failures = check_failures(checks) + reading_level_note(checks)
    for name, score in verdict.scores().items():
        if score.score < config.PASS_MIN_SCORE:
            failures.append(f"{name} scored {score.score}/5")
    return failures


def pick_best(rounds: list[RoundResult]) -> int:
    """Index of the best round: passed first, then no avoid-list hits, then the highest minimum
    score, then the highest mean score, then the later round."""

    def rank(index: int):
        r = rounds[index]
        return (r.passed, not r.checks.avoid_hits, r.verdict.min_score, r.verdict.mean_score, index)

    return max(range(len(rounds)), key=rank)


# --- Pipeline --------------------------------------------------------------------------------


class StoryPipeline:
    def __init__(
        self,
        llm: LLM,
        max_rounds: int = config.MAX_ROUNDS,
        on_progress: Optional[ProgressFn] = None,
        budget: Budget = Budget(),
    ):
        if max_rounds < 1:
            raise ValueError("max_rounds must be at least 1 (one judge round, no revisions)")
        self.llm = llm
        self.max_rounds = max_rounds
        self.budget = budget
        self.on_progress = on_progress

    def _progress(self, step: str, detail: str) -> None:
        if self.on_progress:
            self.on_progress(step, detail)

    # New story ---------------------------------------------------------------------------

    def create(self, request: StoryRequest) -> StorySession:
        started = time.monotonic()
        trace: list[TraceEvent] = []

        self._progress("analyzer", "Understanding the request")
        brief = stages.analyze(self.llm, request, trace)
        session = StorySession(request=request, brief=brief, trace=trace)
        trace = session.trace  # Pydantic copies lists on validation: keep appending to the session's own list
        tracker = BudgetTracker(self.budget, trace, started=started, first_event=0)  # counts the analyzer too
        if brief.status == "out_of_scope":
            self._progress("done", "Not a story request, so I'll suggest one instead")
            return session
        self._progress("analyzer", f"{brief.category.title()} story, {brief.tone}")

        self._progress("writer", "Writing the first draft")
        draft = stages.write(self.llm, request, brief, trace)

        rounds, best, stopped = self.judge_loop(request, brief, draft, brief.adjustments, trace, tracker)
        session.versions.append(
            StoryVersion(
                number=1, origin="initial request", controls=request.controls,
                rounds=rounds, best_round=best, stopped_early=stopped,
            )
        )
        self._progress("done", _finish_summary(rounds, best, stopped))
        return session

    # Judge loop ---------------------------------------------------------------------------

    def judge_loop(
        self,
        request: StoryRequest,
        brief: StoryBrief,
        draft: Draft,
        adjustments: list[str],
        trace: list[TraceEvent],
        tracker: Optional[BudgetTracker] = None,
    ) -> tuple[list[RoundResult], int, Optional[str]]:
        """Judge, revise and repeat until a draft passes, rounds run out or the budget is spent.

        Returns (rounds, index of the best round, why the budget stopped the loop or None).
        """
        tracker = tracker or BudgetTracker(self.budget, trace)
        rounds: list[RoundResult] = []
        stopped: Optional[str] = None
        for number in range(1, self.max_rounds + 1):
            checks = run_checks(draft, request.controls, request.age, request.avoid)
            self._progress("judge", f"Judge round {number}: reviewing {checks.word_count} words")
            verdict = stages.judge(self.llm, request, draft, checks, adjustments, trace)
            failures = gate(verdict, checks)
            rounds.append(
                RoundResult(round=number, draft=draft, checks=checks, verdict=verdict, failures=failures)
            )
            if not failures:
                self._progress("judge", f"Round {number}: passed (lowest score {verdict.min_score}/5)")
                break
            self._progress("judge", f"Round {number}: {len(failures)} to fix ({'; '.join(failures)})")
            if number == self.max_rounds:
                break
            stopped = tracker.exceeded()
            if stopped:
                self._progress("judge", f"Stopping early: {stopped}. Using the best draft so far")
                break
            self._progress("reviser", "Revising with the judge's notes")
            draft = stages.revise(self.llm, request, brief, draft, trace, verdict=verdict, checks=checks)
        return rounds, pick_best(rounds), stopped

    # Follow-ups ---------------------------------------------------------------------------

    def follow_up(
        self,
        session: StorySession,
        message: Optional[str] = None,
        controls: Optional[StoryControls] = None,
        quick_action: Optional[str] = None,
    ) -> FollowUpOutcome:
        """Apply a parent's change: typed message, quick action, new control values, or a mix.

        An edit adds a version to this session. A request for a different story starts a new
        session. Anything out of scope returns a friendly redirect and changes nothing.
        """
        current = session.current
        if current is None:
            raise ValueError("This session has no story to change.")
        tracker = BudgetTracker(self.budget, session.trace)
        old_controls = current.controls
        new_controls = controls or old_controls
        instructions: list[str] = []
        notes: list[str] = []

        if quick_action:
            new_controls, action_instruction = apply_quick_action(quick_action, new_controls)
            instructions.append(action_instruction)

        if message and message.strip():
            self._progress("followup", "Reading your change request")
            intent = stages.route_followup(self.llm, session.request, current.best.draft, message, session.trace)
            if intent.status == "out_of_scope":
                return FollowUpOutcome(kind="redirect", session=session, message=intent.redirect_message)
            if intent.kind == "new":
                new_request = StoryRequest(
                    text=intent.instructions, profile=session.request.profile, controls=new_controls
                )
                new_session = self.create(new_request)
                if new_session.brief.status == "out_of_scope":
                    return FollowUpOutcome(
                        kind="redirect", session=session, message=new_session.brief.redirect_message
                    )
                return FollowUpOutcome(kind="new", session=new_session)
            instructions.append(intent.instructions)
            notes.extend(intent.adjustments)

        instructions.extend(describe_control_changes(old_controls, new_controls))
        if not instructions:
            raise ValueError("Nothing to change: give a message, a quick action or new control values.")
        instruction = "\n".join(f"- {line}" for line in instructions)

        request = _edited_request(session, instruction, new_controls)
        self._progress("reviser", "Applying your changes")
        draft = stages.revise(self.llm, request, session.brief, current.best.draft, session.trace, instruction=instruction)
        rounds, best, stopped = self.judge_loop(
            request, session.brief, draft, session.brief.adjustments + notes, session.trace, tracker
        )
        session.versions.append(
            StoryVersion(
                number=len(session.versions) + 1,
                origin=message.strip() if message and message.strip() else (quick_action or "Updated settings"),
                instruction=instruction,
                controls=new_controls,
                notes=notes,
                rounds=rounds,
                best_round=best,
                stopped_early=stopped,
            )
        )
        self._progress("done", _finish_summary(rounds, best, stopped))
        return FollowUpOutcome(kind="edit", session=session)


def _edited_request(session: StorySession, instruction: str, controls: StoryControls) -> StoryRequest:
    """The original request plus every change asked for so far, so the judge scores the story
    against what the parent now wants, not only what they first asked."""
    changes = [v.instruction for v in session.versions if v.instruction] + [instruction]
    text = session.request.text + "\n\nThe parent then asked for these changes, in order:\n" + "\n".join(changes)
    return session.request.model_copy(update={"text": text, "controls": controls})


def _finish_summary(rounds: list[RoundResult], best: int, stopped: Optional[str] = None) -> str:
    chosen = rounds[best]
    status = "passed" if chosen.passed else "best effort (didn't fully pass)"
    summary = f"Done after {len(rounds)} judge round(s): round {chosen.round} {status}"
    return summary + (f" (stopped early: {stopped})" if stopped else "")
