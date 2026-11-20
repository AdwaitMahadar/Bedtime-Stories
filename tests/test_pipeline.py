"""Pipeline behavior with a scripted LLM: the judge loop, the gate, best-draft selection, follow-ups."""

import pytest

from storyteller.models import ChildProfile, Draft, StoryControls, StoryRequest
from storyteller.pipeline import StoryPipeline, gate, pick_best
from storyteller.checks import check_failures, run_checks

from .fakes import FakeLLM, brief, draft, intent, verdict


def request(**kwargs) -> StoryRequest:
    return StoryRequest(text="A story about Pip the mouse", **kwargs)


def make(llm, **kwargs) -> StoryPipeline:
    return StoryPipeline(llm, **kwargs)


def test_passes_on_first_round_without_revising():
    llm = FakeLLM(analyzer=[brief()], writer=[draft(600)], judge=[verdict(4)])
    session = make(llm).create(request())
    version = session.current
    assert len(version.rounds) == 1
    assert version.best.passed
    assert llm.counts["reviser"] == 0


def test_revises_until_it_passes():
    llm = FakeLLM(
        analyzer=[brief()],
        writer=[draft(600)],
        judge=[verdict(3), verdict(5)],
        reviser=[draft(600, word="cozy")],
    )
    version = make(llm).create(request()).current
    assert [r.passed for r in version.rounds] == [False, True]
    assert version.best_round == 1
    assert "cozy" in version.best.draft.text


def test_stops_at_max_rounds_and_returns_best_not_last():
    llm = FakeLLM(
        analyzer=[brief()],
        writer=[draft(600, word="first")],
        judge=[verdict(3, 4, 4, 4, 4, 4, 4), verdict(2), verdict(3, 3, 4, 4, 4, 4, 4)],
        reviser=[draft(600, word="second"), draft(600, word="third")],
    )
    version = make(llm, max_rounds=3).create(request()).current
    assert len(version.rounds) == 3
    assert not any(r.passed for r in version.rounds)
    assert version.best_round == 0  # round 1 had the highest minimum score
    assert "first" in version.best.draft.text


def test_length_out_of_range_fails_even_with_perfect_scores():
    llm = FakeLLM(
        analyzer=[brief()], writer=[draft(200)],
        judge=[verdict(5), verdict(5)], reviser=[draft(600)],
    )
    version = make(llm, max_rounds=2).create(request()).current
    assert version.rounds[0].failures[0].startswith("Too short")
    assert version.best.passed and version.best_round == 1


def test_avoid_list_hit_is_a_hard_fail_and_ranks_last():
    profile = ChildProfile(name="Mia", age=6, avoid=["spiders"])
    llm = FakeLLM(
        analyzer=[brief()], writer=[draft(600, word="spider")],
        judge=[verdict(5), verdict(3)],
        reviser=[draft(600, word="ladybug")],
    )
    version = make(llm, max_rounds=2).create(request(profile=profile)).current
    first, second = version.rounds
    assert first.checks.avoid_hits == ["spiders"] and not first.passed
    assert version.best_round == 1  # lower scores, but no avoid-list hit


def test_out_of_scope_request_stops_after_the_analyzer():
    llm = FakeLLM(analyzer=[brief(status="out_of_scope")])
    session = make(llm).create(request())
    assert session.versions == []
    assert list(llm.counts) == ["analyzer"]  # no writer call


def test_judge_never_sees_the_brief_or_writer_prompt():
    llm = FakeLLM(analyzer=[brief()], writer=[draft(600)], judge=[verdict(4)])
    make(llm).create(request())
    judge_message = next(user for step, user in llm.calls if step == "judge")
    assert "<brief>" not in judge_message and "<length_plan>" not in judge_message
    assert "<user_request>" in judge_message and "<story>" in judge_message


def test_follow_up_edit_adds_a_version_and_tells_the_judge_about_the_change():
    llm = FakeLLM(
        analyzer=[brief()], writer=[draft(600)],
        judge=[verdict(4), verdict(4)], followup=[intent(instructions="Add a firefly friend.")],
        reviser=[draft(600, word="firefly")],
    )
    pipeline = make(llm)
    session = pipeline.create(request())
    outcome = pipeline.follow_up(session, message="add a firefly")
    assert outcome.kind == "edit" and len(session.versions) == 2
    assert "firefly" in session.current.best.draft.text
    last_judge = [user for step, user in llm.calls if step == "judge"][-1]
    assert "Add a firefly friend." in last_judge


def test_quick_action_changes_controls_without_calling_the_router():
    llm = FakeLLM(
        analyzer=[brief()], writer=[draft(600)],
        judge=[verdict(4), verdict(4)], reviser=[draft(350)],
    )
    pipeline = make(llm)
    session = pipeline.create(request())
    pipeline.follow_up(session, quick_action="Shorter")
    assert session.current.controls.length == "short"
    assert llm.counts["followup"] == 0
    assert session.current.best.checks.length_target == 350


def test_follow_up_new_story_starts_a_new_session():
    llm = FakeLLM(
        analyzer=[brief(), brief()], writer=[draft(600), draft(600)],
        judge=[verdict(4), verdict(4)], followup=[intent(kind="new", instructions="A story about a pirate")],
    )
    pipeline = make(llm)
    session = pipeline.create(request())
    outcome = pipeline.follow_up(session, message="now one about a pirate")
    assert outcome.kind == "new" and outcome.session.id != session.id
    assert outcome.session.request.text == "A story about a pirate"
    assert len(session.versions) == 1


def test_follow_up_out_of_scope_changes_nothing():
    llm = FakeLLM(
        analyzer=[brief()], writer=[draft(600)], judge=[verdict(4)],
        followup=[intent(status="out_of_scope", instructions="")],
    )
    pipeline = make(llm)
    session = pipeline.create(request())
    outcome = pipeline.follow_up(session, message="what's 7 times 8?")
    assert outcome.kind == "redirect" and outcome.message
    assert len(session.versions) == 1


def test_gate_and_pick_best_rules():
    checks = run_checks(draft(600), StoryControls(), age=7, avoid=[])
    assert gate(verdict(4), checks) == []
    assert gate(verdict(4, 4, 4, 4, 4, 4, 3), checks) == ["bedtime_ending scored 3/5"]

    with pytest.raises(ValueError):
        StoryPipeline(FakeLLM(), max_rounds=0)


def test_follow_up_new_story_that_is_out_of_scope_keeps_the_current_story():
    llm = FakeLLM(
        analyzer=[brief(), brief(status="out_of_scope")], writer=[draft(600)],
        judge=[verdict(4)], followup=[intent(kind="new", instructions="something unsuitable")],
    )
    pipeline = make(llm)
    session = pipeline.create(request())
    outcome = pipeline.follow_up(session, message="tell me something else")
    assert outcome.kind == "redirect" and outcome.session is session and outcome.message


def test_session_trace_records_every_call():
    llm = FakeLLM(
        analyzer=[brief()], writer=[draft(600)],
        judge=[verdict(3), verdict(4)], reviser=[draft(600)],
    )
    session = make(llm).create(request())
    assert [e.step for e in session.trace] == ["analyzer", "writer", "judge", "reviser", "judge"]


def test_reading_level_above_the_age_band_sends_the_draft_back():
    hard = Draft(title="T", text=" ".join(["Considerable extraordinary circumstances necessitated unanticipated deliberation."] * 60))
    checks = run_checks(hard, StoryControls(length="short"), age=5, avoid=[])
    assert checks.reading_grade > checks.grade_max
    failures = gate(verdict(5), checks)
    assert any(f.startswith("Reading level too high") for f in failures)
    assert check_failures(checks) == []  # the eval's pass definition doesn't include reading level


def test_writer_gets_the_parent_facing_notes_but_not_the_full_brief():
    llm = FakeLLM(
        analyzer=[brief(assumptions=["I chose a hedgehog named Tansy as the hero."])],
        writer=[draft(600)], judge=[verdict(4)],
    )
    make(llm).create(request())
    writer_message = next(user for step, user in llm.calls if step == "writer")
    assert "<decisions>" in writer_message and "Tansy" in writer_message
    assert "<brief>" not in writer_message


# --- Per-story budget ---------------------------------------------------------------------------


def test_budget_stops_revising_and_returns_the_best_draft_so_far():
    from storyteller.budget import Budget

    # analyzer + writer + judge = 3 calls: the budget is spent before the first revision
    llm = FakeLLM(analyzer=[brief()], writer=[draft(600)], judge=[verdict(3)])
    session = make(llm, budget=Budget(max_calls=3)).create(request())
    version = session.current
    assert len(version.rounds) == 1 and llm.counts["reviser"] == 0
    assert version.stopped_early.startswith("call limit reached (3 of 3")
    assert version.best.draft.text == draft(600).text


def test_budget_for_a_follow_up_counts_only_that_follow_up():
    from storyteller.budget import Budget

    llm = FakeLLM(
        analyzer=[brief()], writer=[draft(600)], judge=[verdict(4), verdict(3), verdict(4)],
        reviser=[draft(350), draft(350)],
    )
    pipeline = make(llm, budget=Budget(max_calls=4))
    session = pipeline.create(request())  # 3 calls
    pipeline.follow_up(session, quick_action="Shorter")  # reviser + judge (fails) + reviser + judge = 4
    version = session.current
    assert len(version.rounds) == 2 and version.stopped_early is None
    assert len(session.trace) == 7


def test_budget_tracker_limits_by_cost_and_time():
    from storyteller.budget import Budget, BudgetTracker
    from storyteller.models import TraceEvent

    now = [0.0]
    trace: list[TraceEvent] = []
    tracker = BudgetTracker(Budget(max_calls=10, max_cost_usd=0.01, max_seconds=60), trace, clock=lambda: now[0])
    assert tracker.exceeded() is None
    trace.append(TraceEvent(step="writer", model="m", attempt=1, ok=True, cost_usd=0.012))
    assert tracker.exceeded().startswith("cost limit reached")
    trace.clear()
    now[0] = 61
    assert tracker.exceeded().startswith("time limit reached")
