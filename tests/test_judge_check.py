"""The judge check with a scripted LLM: detection, false alarms and the report."""

from eval.judge_check import check_case, load_cases, render_report, summarize

from .fakes import FakeLLM, failing, verdict


def test_cases_are_valid_and_planted_kinds_are_on_the_checklist():
    cases = load_cases()
    kinds = {c["planted"] for c in cases}
    assert {
        "stated_moral",
        "consistency",
        "safety",
        "missing_request",
        "notes_mismatch",
        "ending",
        "age",
        None,
    } == kinds
    for case in cases:  # every case builds into a valid request, story and checks
        result = check_case(FakeLLM(judge=[verdict(4)]), case, samples=1, trace=[])
        assert result["calls"][0]["blocked"] is False


def test_detection_and_false_alarms_are_counted_per_call():
    cases = {c["id"]: c for c in load_cases()}
    planted = check_case(FakeLLM(judge=[failing("stated_moral"), verdict(4)]), cases["planted-stated-moral"], 2, [])
    clean = check_case(FakeLLM(judge=[failing("safety"), verdict(4)]), cases["clean-sunflowers"], 2, [])
    s = summarize([planted, clean])
    assert s["caught_right_kind"] == 0.5 and s["caught"] == 0.5 and s["false_alarms"] == 0.5
    report = render_report([planted, clean], "test", 2)
    assert "| planted-stated-moral | stated_moral | stated_moral / nothing |" in report
