"""Orchestration: request check -> writer -> [code checks -> judge -> reviser]* -> best draft.

The pass/fail decision (`gate`) and the choice of the best draft (`pick_best`) are plain functions,
so the rules are explicit and unit-tested rather than hidden in a prompt. A draft passes when the
judge finds no must-fix issue and the code checks pass; the judge's 1-5 scores are for display and
the eval, except a score of 2 or less, which fails the draft. Each run (a new story or a follow-up)
is capped by a per-story `Budget` of calls, cost and time.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from . import config, stages
from .budget import Budget, BudgetTracker
from .checks import avoid_failures, find_avoid_hits, length_failures, reading_level_note, run_checks
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


UNCHANGED = (
    "Unchanged: the story is word for word the same as before, so the parent's request wasn't applied. "
    "Make the change they asked for."
)


def gate(verdict: JudgeVerdict, checks) -> list[str]:
    """Problems that stop a draft from passing, or an empty list.

    Blocks on: any must-fix issue from the judge, any criterion scored FAIL_MAX_SCORE or lower, or an
    avoid-list hit. Length and reading level are `soft_failures`: they get a revision, but a story that
    is a little short is still a good story (testing showed "Best effort" on good stories 11 words short).
    """
    failures = avoid_failures(checks)
    failures += [f'{issue.kind}: "{issue.quote}"' for issue in verdict.issues]
    failures += [
        f"{name} scored {score.score}/5"
        for name, score in verdict.scores().items()
        if score.score <= config.FAIL_MAX_SCORE
    ]
    return failures


def soft_failures(checks) -> list[str]:
    """Length outside the range, or a reading grade clearly above the age band."""
    return length_failures(checks) + reading_level_note(checks)


def same_text(a: str, b: str) -> bool:
    return " ".join(a.split()) == " ".join(b.split())


def pick_best(rounds: list[RoundResult]) -> int:
    """Index of the best round: passed first, then no avoid-list hits, then the fewest problems,
    then the fewest soft problems, then the highest mean score, then the later round."""

    def rank(index: int):
        r = rounds[index]
        return (
            r.passed,
            not r.checks.avoid_hits,
            -len(r.failures),
            -len(r.soft_failures),
            r.verdict.mean_score,
            index,
        )

    return max(range(len(rounds)), key=rank)


# --- Pipeline --------------------------------------------------------------------------------


class StoryPipeline:
    def __init__(
        self,
        llm: LLM,
        max_rounds: int = config.MAX_ROUNDS,
        on_progress: ProgressFn | None = None,
        budget: Budget | None = None,
    ):
        if max_rounds < 1:
            raise ValueError("max_rounds must be at least 1 (one judge round, no revisions)")
        self.llm = llm
        self.max_rounds = max_rounds
        self.budget = budget or Budget()
        self.on_progress = on_progress

    def _progress(self, step: str, detail: str) -> None:
        if self.on_progress:
            self.on_progress(step, detail)

    # New story ---------------------------------------------------------------------------

    def create(self, request: StoryRequest) -> StorySession:
        started = time.monotonic()
        trace: list[TraceEvent] = []

        self._progress("analyzer", "Checking the request")
        brief = stages.analyze(self.llm, request, trace)
        session = StorySession(request=request, brief=brief, trace=trace)
        trace = session.trace  # Pydantic copies lists on validation: keep appending to the session's own list
        tracker = BudgetTracker(self.budget, trace, started=started, first_event=0)  # counts the analyzer too
        if brief.status == "out_of_scope":
            self._progress("done", "Not a story request, so I'll suggest one instead")
            return session
        if brief.status == "adjusted":
            self._progress("analyzer", "Softening part of the request to suit bedtime")

        self._progress("writer", "Writing the first draft")
        draft = _notes_only_for_changes(stages.write(self.llm, request, brief, trace), brief.adjustments)

        rounds, best, stopped = self.judge_loop(request, brief, draft, brief.adjustments, trace, tracker)
        session.versions.append(
            StoryVersion(
                number=1,
                origin="initial request",
                controls=request.controls,
                rounds=rounds,
                best_round=best,
                stopped_early=stopped,
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
        tracker: BudgetTracker | None = None,
        parent_request: str | None = None,
        previous_text: str | None = None,
    ) -> tuple[list[RoundResult], int, str | None]:
        """Judge, revise and repeat until a draft has nothing left to fix, rounds run out or the
        budget is spent. For a follow-up, `previous_text` catches a revision that changed nothing
        ("make it scarier" once came back word for word the same), and `parent_request` reaches
        every revision.

        Returns (rounds, index of the best round, why the budget stopped the loop or None).
        """
        tracker = tracker or BudgetTracker(self.budget, trace)
        rounds: list[RoundResult] = []
        stopped: str | None = None
        for number in range(1, self.max_rounds + 1):
            checks = run_checks(draft, request.controls, request.age, request.avoid, brief.language)
            self._progress("judge", f"Review {number}: checking {checks.word_count} words")
            verdict = stages.judge(
                self.llm, request, draft, checks, adjustments, trace, sensitive_topic=brief.sensitive_topic
            )
            failures = gate(verdict, checks)
            unchanged = number == 1 and previous_text is not None and same_text(draft.text, previous_text)
            if unchanged:
                failures.insert(0, UNCHANGED)
            soft = soft_failures(checks)
            rounds.append(
                RoundResult(
                    round=number, draft=draft, checks=checks, verdict=verdict, failures=failures, soft_failures=soft
                )
            )
            if not failures and not soft:
                self._progress("judge", f"Review {number}: no problems found")
                break
            to_fix = failures + soft
            self._progress("judge", f"Review {number}: {len(to_fix)} to fix ({'; '.join(to_fix)})")
            if number == self.max_rounds:
                break
            stopped = tracker.exceeded()
            if stopped:
                self._progress("judge", f"Stopping early: {stopped}. Using the best draft so far")
                break
            self._progress("reviser", "Fixing what the review found")
            draft = stages.revise(
                self.llm,
                request,
                draft,
                adjustments,
                trace,
                verdict=verdict,
                checks=checks,
                parent_request=parent_request,
                extra_failures=[UNCHANGED] if unchanged else [],
            )
            draft = _notes_only_for_changes(draft, adjustments)
        return rounds, pick_best(rounds), stopped

    # Follow-ups ---------------------------------------------------------------------------

    def follow_up(
        self,
        session: StorySession,
        message: str | None = None,
        controls: StoryControls | None = None,
        quick_action: str | None = None,
    ) -> FollowUpOutcome:
        """Apply a parent's change: typed message, quick action, new control values, or a mix.

        An edit adds a version to this session. A request for a different story starts a new
        session. Anything out of scope returns a friendly redirect and changes nothing. The parent's
        message reaches the reviser in their own words; the router only classifies it.
        """
        current = session.current
        if current is None:
            raise ValueError("This session has no story to change.")
        tracker = BudgetTracker(self.budget, session.trace)
        old_controls = current.controls
        new_controls = controls or old_controls
        changes: list[str] = []
        adjustments: list[str] = []
        words = message.strip() if message and message.strip() else None

        if quick_action:
            new_controls, action_instruction = apply_quick_action(quick_action, new_controls)
            changes.append(action_instruction)

        if words:
            self._progress("followup", "Reading your change request")
            intent = stages.route_followup(self.llm, session.request, current.best.draft, words, session.trace)
            if intent.status == "out_of_scope":
                return FollowUpOutcome(kind="redirect", session=session, message=intent.redirect_message)
            if intent.kind == "new":
                new_request = StoryRequest(text=words, profile=session.request.profile, controls=new_controls)
                new_session = self.create(new_request)
                if new_session.brief.status == "out_of_scope":
                    return FollowUpOutcome(kind="redirect", session=session, message=new_session.brief.redirect_message)
                return FollowUpOutcome(kind="new", session=new_session)
            # The avoid-list is enforced in code, whatever the router decided.
            adjustments += [
                f'"{term}" is on the child\'s avoid-list: leave it out completely, and if the parent '
                "asked to add it, use something else that fits instead."
                for term in find_avoid_hits(words, session.request.avoid)
            ]
            adjustments += intent.adjustments
            changes.append(f'The parent said: "{words}"')

        changes.extend(describe_control_changes(old_controls, new_controls))
        if not changes:
            raise ValueError("Nothing to change: give a message, a quick action or new control values.")
        parent_request = "\n".join(f"- {line}" for line in changes)

        request = _edited_request(session, parent_request, new_controls)
        all_adjustments = session.brief.adjustments + adjustments
        self._progress("reviser", "Applying your changes")
        draft = stages.revise(
            self.llm, request, current.best.draft, all_adjustments, session.trace, parent_request=parent_request
        )
        draft = _notes_only_for_changes(draft, all_adjustments)
        rounds, best, stopped = self.judge_loop(
            request,
            session.brief,
            draft,
            all_adjustments,
            session.trace,
            tracker,
            parent_request=parent_request,
            previous_text=current.best.draft.text,
        )
        session.versions.append(
            StoryVersion(
                number=len(session.versions) + 1,
                origin=words or quick_action or "Updated settings",
                instruction=parent_request,
                controls=new_controls,
                adjustments=adjustments,
                rounds=rounds,
                best_round=best,
                stopped_early=stopped,
            )
        )
        self._progress("done", _finish_summary(rounds, best, stopped))
        return FollowUpOutcome(kind="edit", session=session)


def _notes_only_for_changes(draft: Draft, adjustments: list[str]) -> Draft:
    """Parent notes exist only to explain a change from the request (softening, the avoid-list).

    Enforced in code because the model tends to add chatty notes anyway ("I deepened the
    middle...", "using their trains and volcanoes as favorite-thing touches").
    """
    if adjustments or not draft.notes:
        return draft
    return draft.model_copy(update={"notes": []})


def _edited_request(session: StorySession, instruction: str, controls: StoryControls) -> StoryRequest:
    """The original request plus every change asked for so far, so the judge scores the story
    against what the parent now wants, not only what they first asked."""
    changes = [v.instruction for v in session.versions if v.instruction] + [instruction]
    text = session.request.text + "\n\nThe parent then asked for these changes, in order:\n" + "\n".join(changes)
    return session.request.model_copy(update={"text": text, "controls": controls})


def _finish_summary(rounds: list[RoundResult], best: int, stopped: str | None = None) -> str:
    chosen = rounds[best]
    status = "passed review" if chosen.passed else f"best effort ({len(chosen.failures)} problem(s) left)"
    if chosen.passed and chosen.soft_failures:
        status += " (length or reading level slightly off)"
    summary = f"Done after {len(rounds)} review(s): draft {chosen.round} {status}"
    return summary + (f" (stopped early: {stopped})" if stopped else "")
