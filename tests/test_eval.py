"""The eval runner end to end with a scripted LLM: scoring, out-of-scope cases and the report."""

from eval.run_eval import render_report, render_stories, run_case

from .fakes import FakeLLM, brief, draft, failing, verdict


def test_run_case_scores_the_first_draft_and_the_final_story_with_fresh_judge_calls():
    llm = FakeLLM(
        analyzer=[brief()],
        writer=[draft(600, word="first")],
        # judge calls in order: loop rounds 1-2, then the first-draft and final re-scores
        judge=[failing(), verdict(4), failing("safety", "ending"), verdict(5)],
        reviser=[draft(600, word="final")],
    )
    case = {"id": "c1", "tag": "normal", "request": "Pip the mouse", "expected_status": "ok"}
    result = run_case(llm, case, max_rounds=3)

    assert set(result["variants"]) == {"first_draft", "final"}
    first, final = result["variants"]["first_draft"], result["variants"]["final"]
    assert first["text"].startswith("first") and first["issues"] == 2 and first["passed"] == 0
    assert final["text"].startswith("final") and final["issues"] == 0 and final["passed"] == 1
    assert result["rounds"] == 2 and llm.counts["judge"] == 4

    report = render_report([result], "test", 3)
    assert "Writer only (no judge)" in report and "With judge loop" in report and "| c1 | normal | ok (ok)" in report
    assert "Must-fix issues per story | 2.00 | 0.00" in report and "| safety | 1 | 0 |" in report
    stories = render_stories([result])
    assert "Writer only (no judge)" in stories and "With judge loop" in stories and "final is a pup" in stories


def test_out_of_scope_case_is_reported_without_scores():
    llm = FakeLLM(analyzer=[brief(status="out_of_scope")])
    case = {"id": "hw", "tag": "off-topic", "request": "math homework", "expected_status": "out_of_scope"}
    result = run_case(llm, case, max_rounds=3)
    assert result["status"] == "out_of_scope" and result["redirect_message"]
    assert "final" not in result["variants"]
    assert "out_of_scope (out_of_scope)" in render_report([result], "test", 3)


def test_multiple_judge_samples_are_averaged_and_agreement_is_measured():
    from eval.run_eval import score
    from storyteller.models import StoryRequest

    llm = FakeLLM(judge=[verdict(4), failing("age")])
    result = score(llm, StoryRequest(text="Pip"), draft(600), trace=[], samples=2)
    assert result["scores"]["age_fit"] == 4 and result["issues"] == 0.5
    assert result["passed"] == 0.5  # one of the two calls found a must-fix issue
    assert result["agreement"] == 1 and result["pass_flip"] is True


def test_eval_judge_sees_the_same_adjustments_as_the_in_app_judge():
    llm = FakeLLM(
        analyzer=[brief(status="adjusted", adjustments=["The monster eats cookies instead of people."])],
        writer=[draft(600)],
        judge=[verdict(4), verdict(4), verdict(4)],
    )
    case = {"id": "m", "tag": "unsafe", "request": "a monster that eats people", "expected_status": "adjusted"}
    result = run_case(llm, case, max_rounds=3)
    judge_messages = [user for step, user in llm.calls if step == "judge"]
    assert len(judge_messages) == 3 and all("eats cookies instead of people" in m for m in judge_messages)
    assert "Same text scored twice: in 1 cases" in render_report([result], "test", 3)
