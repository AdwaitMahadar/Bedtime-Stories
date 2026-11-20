"""The QA harness with a scripted LLM: sessions with follow-ups are written out for reading."""

import json

from eval.qa_run import CASES, run_case, word_diff
from storyteller.pipeline import StoryPipeline

from .fakes import FakeLLM, brief, draft, intent, verdict


def test_a_case_with_follow_ups_is_written_out_with_every_draft(tmp_path):
    llm = FakeLLM(
        analyzer=[brief()],
        writer=[draft(600)],
        judge=[verdict(4), verdict(4), verdict(4)],
        reviser=[draft(600, word="funny"), draft(600, word="pigeon")],
        followup=[intent()],
    )
    result = run_case(StoryPipeline(llm), "horse-city", tmp_path)  # Funnier, then a typed change
    assert result["versions"] == 3 and result["best_effort"] == 0
    text = (tmp_path / "horse-city.md").read_text()
    assert "## Version 3: Make the pigeon a boy named Pip" in text
    assert "DIFF: " in text and "{+funny" in text  # later drafts are shown as word diffs
    saved = json.loads((tmp_path / "horse-city-main.json").read_text())
    assert len(saved["versions"]) == 3


def test_word_diff_marks_removed_and_added_words():
    assert word_diff("the cat sat", "the dog sat") == "the [-cat-] {+dog+} sat"


def test_every_case_is_a_valid_request():
    assert len(CASES) == 18 and sum(len(case[3]) for case in CASES.values()) == 10
