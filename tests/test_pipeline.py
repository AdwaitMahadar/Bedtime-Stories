"""Pipeline behavior with a scripted LLM: the judge loop, the gate, best-draft selection, follow-ups."""

import pytest

from storyteller.models import ChildProfile, Draft, StoryControls, StoryRequest
from storyteller.pipeline import UNCHANGED, StoryPipeline, gate, pick_best, soft_failures
from storyteller.checks import check_failures, run_checks

from .fakes import FakeLLM, brief, draft, failing, intent, verdict


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
        judge=[failing(), verdict(5)],
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
        judge=[failing("consistency"), failing("safety", "ending"), failing("age", "ending")],
        reviser=[draft(600, word="second"), draft(600, word="third")],
    )
    version = make(llm, max_rounds=3).create(request()).current
    assert len(version.rounds) == 3
    assert not any(r.passed for r in version.rounds)
    assert version.best_round == 0  # round 1 had the fewest problems
    assert "first" in version.best.draft.text


def test_length_out_of_range_gets_a_revision_but_does_not_block_passing():
    llm = FakeLLM(
        analyzer=[brief()], writer=[draft(200)],
        judge=[verdict(5), verdict(5)], reviser=[draft(600)],
    )
    version = make(llm, max_rounds=2).create(request()).current
    first, second = version.rounds
    assert first.passed and not first.clean and first.soft_failures[0].startswith("Too short")
    assert second.clean and version.best_round == 1  # in range beats slightly short


def test_a_story_still_short_after_revising_passes_and_is_not_best_effort():
    llm = FakeLLM(
        analyzer=[brief()], writer=[draft(200)],
        judge=[verdict(5), verdict(5)], reviser=[draft(300)],
    )
    version = make(llm, max_rounds=2).create(request()).current
    assert len(version.rounds) == 2 and version.best.passed and version.best.soft_failures


def test_avoid_list_hit_is_a_hard_fail_and_ranks_last():
    profile = ChildProfile(name="Mia", age=6, avoid=["spiders"])
    llm = FakeLLM(
        analyzer=[brief()], writer=[draft(600, word="spider")],
        judge=[verdict(5), failing()],
        reviser=[draft(600, word="ladybug")],
    )
    version = make(llm, max_rounds=2).create(request(profile=profile)).current
    first, second = version.rounds
    assert first.checks.avoid_hits == ["spiders"] and not first.passed
    assert version.best_round == 1  # a judge issue, but no avoid-list hit


def test_out_of_scope_request_stops_after_the_analyzer():
    llm = FakeLLM(analyzer=[brief(status="out_of_scope")])
    session = make(llm).create(request())
    assert session.versions == []
    assert list(llm.counts) == ["analyzer"]  # no writer call


def test_judge_sees_the_story_and_its_notes_but_never_the_writer_prompt():
    llm = FakeLLM(
        analyzer=[brief(status="adjusted", adjustments=["Soften the monster."])],
        writer=[draft(600, notes=["The monster only eats cookies."])], judge=[verdict(4)],
    )
    make(llm).create(request())
    judge_message = next(user for step, user in llm.calls if step == "judge")
    assert "<length_plan>" not in judge_message
    assert "<user_request>" in judge_message and "<story>" in judge_message
    assert "<parent_notes>" in judge_message and "The monster only eats cookies." in judge_message


def test_follow_up_edit_sends_the_parents_own_words_to_the_reviser_and_judge():
    llm = FakeLLM(
        analyzer=[brief()], writer=[draft(600)],
        judge=[verdict(4), verdict(4)], followup=[intent()],
        reviser=[draft(600, word="firefly")],
    )
    pipeline = make(llm)
    session = pipeline.create(request())
    outcome = pipeline.follow_up(session, message="make the pigeon a boy named Pip")
    assert outcome.kind == "edit" and len(session.versions) == 2
    assert "firefly" in session.current.best.draft.text
    reviser_message = next(user for step, user in llm.calls if step == "reviser")
    assert 'The parent said: "make the pigeon a boy named Pip"' in reviser_message  # verbatim, not paraphrased
    last_judge = [user for step, user in llm.calls if step == "judge"][-1]
    assert "make the pigeon a boy named Pip" in last_judge


def test_follow_up_asking_for_an_avoid_list_item_is_guarded_in_code():
    profile = ChildProfile(name="Mia", age=6, avoid=["spiders"])
    llm = FakeLLM(
        analyzer=[brief()], writer=[draft(600)], judge=[verdict(4), verdict(4)],
        followup=[intent()],  # the router misses the conflict
        reviser=[draft(600, word="beetle")],
    )
    pipeline = make(llm)
    session = pipeline.create(request(profile=profile))
    pipeline.follow_up(session, message="Add a big hairy spider who becomes her friend")
    reviser_message = next(user for step, user in llm.calls if step == "reviser")
    assert '"spiders" is on the child\'s avoid-list' in reviser_message
    assert session.current.adjustments and "spiders" in session.current.adjustments[0]


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
        judge=[verdict(4), verdict(4)], followup=[intent(kind="new")],
    )
    pipeline = make(llm)
    session = pipeline.create(request())
    outcome = pipeline.follow_up(session, message="now one about a pirate")
    assert outcome.kind == "new" and outcome.session.id != session.id
    assert outcome.session.request.text == "now one about a pirate"  # the parent's own words
    assert len(session.versions) == 1


def test_follow_up_out_of_scope_changes_nothing():
    llm = FakeLLM(
        analyzer=[brief()], writer=[draft(600)], judge=[verdict(4)],
        followup=[intent(status="out_of_scope")],
    )
    pipeline = make(llm)
    session = pipeline.create(request())
    outcome = pipeline.follow_up(session, message="what's 7 times 8?")
    assert outcome.kind == "redirect" and outcome.message
    assert len(session.versions) == 1


def test_gate_and_pick_best_rules():
    checks = run_checks(draft(600), StoryControls(), age=7, avoid=[])
    assert gate(verdict(4), checks) == []
    assert gate(verdict(3), checks) == []  # matters of taste don't fail a draft
    assert gate(verdict(4, 4, 4, 4, 4, 4, 2), checks) == ["bedtime_ending scored 2/5"]
    assert gate(failing("safety"), checks) == ['safety: "quoted line"']
    nearly = run_checks(draft(480), StoryControls(), age=7, avoid=[])  # 20% short: within tolerance
    assert gate(verdict(4), nearly) == []

    with pytest.raises(ValueError):
        StoryPipeline(FakeLLM(), max_rounds=0)


def test_follow_up_new_story_that_is_out_of_scope_keeps_the_current_story():
    llm = FakeLLM(
        analyzer=[brief(), brief(status="out_of_scope")], writer=[draft(600)],
        judge=[verdict(4)], followup=[intent(kind="new")],
    )
    pipeline = make(llm)
    session = pipeline.create(request())
    outcome = pipeline.follow_up(session, message="tell me something else")
    assert outcome.kind == "redirect" and outcome.session is session and outcome.message


def test_session_trace_records_every_call():
    llm = FakeLLM(
        analyzer=[brief()], writer=[draft(600)],
        judge=[failing(), verdict(4)], reviser=[draft(600)],
    )
    session = make(llm).create(request())
    assert [e.step for e in session.trace] == ["analyzer", "writer", "judge", "reviser", "judge"]


def test_reading_level_clearly_above_the_age_band_sends_an_english_draft_back():
    hard = Draft(title="T", notes=[], text=" ".join(["Considerable extraordinary circumstances necessitated unanticipated deliberation."] * 60))
    checks = run_checks(hard, StoryControls(length="short"), age=5, avoid=[])
    assert checks.reading_grade > checks.grade_max + 1
    assert gate(verdict(5), checks) == []  # never blocks a pass on its own
    assert any(f.startswith("Reading level too high") for f in soft_failures(checks))
    assert check_failures(checks) == []  # the eval's pass definition doesn't include reading level
    spanish = run_checks(hard, StoryControls(length="short"), age=5, avoid=[], language="es")
    assert not any(f.startswith("Reading level") for f in soft_failures(spanish))  # English-only formula


def test_writer_gets_the_request_checks_adjustments_topic_and_language():
    llm = FakeLLM(
        analyzer=[brief(status="adjusted", adjustments=["The monster eats cookies, not people."],
                        sensitive_topic="a new baby brother", language="es")],
        writer=[draft(600)], judge=[verdict(4)],
    )
    session = make(llm).create(request())
    writer_message = next(user for step, user in llm.calls if step == "writer")
    assert "The monster eats cookies, not people." in writer_message
    assert "a new baby brother" in writer_message and "<language>\nes\n</language>" in writer_message
    assert session.current.best.checks.language == "es"


# --- Per-story budget ---------------------------------------------------------------------------


def test_budget_stops_revising_and_returns_the_best_draft_so_far():
    from storyteller.budget import Budget

    # analyzer + writer + judge = 3 calls: the budget is spent before the first revision
    llm = FakeLLM(analyzer=[brief()], writer=[draft(600)], judge=[failing()])
    session = make(llm, budget=Budget(max_calls=3)).create(request())
    version = session.current
    assert len(version.rounds) == 1 and llm.counts["reviser"] == 0
    assert version.stopped_early.startswith("call limit reached (3 of 3")
    assert version.best.draft.text == draft(600).text


def test_budget_for_a_follow_up_counts_only_that_follow_up():
    from storyteller.budget import Budget

    llm = FakeLLM(
        analyzer=[brief()], writer=[draft(600)], judge=[verdict(4), failing(), verdict(4)],
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


def test_parent_notes_are_kept_only_when_the_request_was_changed():
    chatty = draft(600, notes=["I chose a hedgehog as the hero."])
    llm = FakeLLM(analyzer=[brief()], writer=[chatty], judge=[verdict(4)])
    assert make(llm).create(request()).current.best.draft.notes == []

    softened = draft(600, notes=["The monster only eats cookies."])
    llm = FakeLLM(analyzer=[brief(status="adjusted", adjustments=["Soften the monster."])], writer=[softened], judge=[verdict(4)])
    assert make(llm).create(request()).current.best.draft.notes == ["The monster only eats cookies."]


def test_a_follow_up_that_changes_nothing_is_sent_back_with_the_parents_words():
    llm = FakeLLM(
        analyzer=[brief()], writer=[draft(600)], judge=[verdict(4), verdict(4), verdict(4)],
        followup=[intent()],
        reviser=[draft(600), draft(600, word="spooky")],  # first revision returns the same story
    )
    pipeline = make(llm)
    session = pipeline.create(request())
    pipeline.follow_up(session, message="Make it scarier")
    version = session.current
    assert version.rounds[0].failures == [UNCHANGED] and not version.rounds[0].passed
    second_revision = [user for step, user in llm.calls if step == "reviser"][-1]
    assert "Make it scarier" in second_revision and "Unchanged" in second_revision
    assert version.best_round == 1 and "spooky" in version.best.draft.text


def test_judge_is_told_the_real_life_topic_and_writer_gets_name_ideas():
    llm = FakeLLM(analyzer=[brief(sensitive_topic="our dog died")], writer=[draft(600)], judge=[verdict(4)])
    make(llm).create(request())
    judge_message = next(user for step, user in llm.calls if step == "judge")
    writer_message = next(user for step, user in llm.calls if step == "writer")
    assert "<sensitive_topic>\nour dog died" in judge_message
    assert "<name_ideas>" in writer_message


def test_name_ideas_are_varied_and_never_the_overused_favourites():
    import random
    from storyteller.names import NAMES, name_ideas

    assert len(set(NAMES)) == len(NAMES) and not {"Pip", "Pim", "Biscuit"} & set(NAMES)
    picks = name_ideas(5, random.Random(1))
    assert len(set(picks)) == 5 and picks != name_ideas(5, random.Random(2))


def test_draft_schema_puts_the_story_before_the_title():
    assert list(Draft.model_fields)[:2] == ["text", "title"]


def test_stray_closing_brackets_from_the_model_are_trimmed():
    from storyteller.models import tidy

    assert tidy("How about a story instead?}") == "How about a story instead?"
    assert tidy("  The end.]  ") == "The end."
    assert tidy("A box {of buttons}") == "A box {of buttons}"  # balanced brackets are kept
    assert brief(status="out_of_scope", redirect_message="Let's tell a story.}").redirect_message == "Let's tell a story."
    assert Draft(text="Goodnight.}", title="Moon}", notes=["Note.]"]).text == "Goodnight."
