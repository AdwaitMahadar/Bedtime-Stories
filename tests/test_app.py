"""The Streamlit UI, driven headless with Streamlit's AppTest and a scripted LLM (no network)."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import storyteller.llm as llm_module
from app import md, pick_reason, story_html
from storyteller import config
from storyteller.controls import CONTROL_LABELS, ENERGY, LESSON, SILLINESS
from storyteller.llm import APICallError, KeyStatus
from storyteller.models import StoryVersion
from storyteller.pipeline import StoryPipeline

from .fakes import FakeLLM, brief, draft, failing, intent, verdict
from .test_pipeline import request

APP = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture(autouse=True)
def log_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")


def start(llm) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=10)
    at.session_state["llm"] = llm  # skips the key check
    return at.run()


def button(at: AppTest, label: str):
    return next(b for b in at.button if b.label == label)


def tell_story(at: AppTest, text: str = "A story about Pip the mouse") -> AppTest:
    at.text_area(key="request_text").input(text)
    return button(at, "Tell me a story").click().run()


def story_text(at: AppTest) -> str:
    return next(m.value for m in at.markdown if 'class="story"' in m.value)


# --- Key check -----------------------------------------------------------------------------


def test_missing_key_blocks_the_app_until_retry_finds_one(monkeypatch):
    monkeypatch.setattr(llm_module, "load_api_key", lambda: None)
    at = AppTest.from_file(APP).run()
    assert "No API key found" in at.error[0].value
    assert [b.label for b in at.button] == ["Retry"]
    assert not at.text_area

    monkeypatch.setattr(llm_module, "load_api_key", lambda: "sk-test")
    monkeypatch.setattr(llm_module, "check_api_key", lambda key: KeyStatus(True))
    at = button(at, "Retry").click().run()
    assert at.text_area(key="request_text")


def test_rejected_key_shows_its_own_message(monkeypatch):
    monkeypatch.setattr(llm_module, "load_api_key", lambda: "sk-bad")
    monkeypatch.setattr(
        llm_module, "check_api_key", lambda key: KeyStatus(False, "invalid", "The API key was rejected.")
    )
    at = AppTest.from_file(APP).run()
    assert at.subheader[0].value == "The API key didn't work"
    assert "rejected" in at.error[0].value


# --- Story flow --------------------------------------------------------------------------------


def test_tells_a_story_with_notes_and_adds_it_to_the_library():
    llm = FakeLLM(
        analyzer=[brief(status="adjusted", adjustments=["Make the monster friendly."])],
        writer=[draft(600, notes=["The monster is friendly and only eats pancakes."])],
        judge=[verdict(4)],
    )
    at = tell_story(start(llm))
    assert at.header[0].value == "Pip and the Blanket"
    assert "calm is a pup" in story_text(at)
    assert "only eats pancakes" in at.info[0].value
    assert any(b.label == "Pip and the Blanket" for b in at.sidebar.button)


def test_quick_action_adds_a_version_and_earlier_versions_stay_readable():
    llm = FakeLLM(
        analyzer=[brief()],
        writer=[draft(600)],
        judge=[verdict(4), verdict(4)],
        reviser=[draft(350, title="Pip, Shorter")],
    )
    at = tell_story(start(llm))
    at = button(at, "Shorter").click().run()
    assert at.header[0].value == "Pip, Shorter"
    assert "Make the story shorter" in llm.calls[-2][1]  # the reviser got the quick action's instruction

    version_picker = next(s for s in at.segmented_control if s.label == "Version")
    at = version_picker.set_value(1).run()
    assert at.header[0].value == "Pip and the Blanket"
    assert "latest version, v2" in at.info[-1].value


def test_out_of_scope_request_gets_a_redirect_and_no_story():
    llm = FakeLLM(analyzer=[brief(status="out_of_scope")])
    at = tell_story(start(llm), "Help me with my math homework")
    assert "Let's make a story instead!" in at.info[0].value
    assert not at.header
    assert at.session_state["sessions"] == []


def test_off_topic_follow_up_leaves_the_story_unchanged():
    llm = FakeLLM(
        analyzer=[brief()],
        writer=[draft(600)],
        judge=[verdict(4)],
        followup=[intent(status="out_of_scope")],
    )
    at = tell_story(start(llm))
    at.text_input[0].input("What's the weather tomorrow?")
    at = button(at, "Update story").click().run()
    assert "Let's get back to the story!" in at.info[-1].value
    assert len(at.session_state["sessions"][0].versions) == 1


def test_api_error_shows_a_friendly_message_and_keeps_the_request():
    class FailingLLM:
        def structured(self, **_):
            raise APICallError("network", "Couldn't reach Anthropic.")

    at = tell_story(start(FailingLLM()), "A story about a brave snail")
    assert "Couldn't reach Anthropic" in at.error[0].value
    assert at.text_area(key="request_text").value == "A story about a brave snail"
    assert not at.exception


# --- Helpers -----------------------------------------------------------------------------------


def test_story_and_model_text_is_escaped():
    assert "<script>" not in story_html("Hi <script>alert(1)</script>\n\nBye")
    assert story_html("One\n\nTwo").count("<p>") == 2
    assert "<em>whoosh</em>" in story_html("It went *whoosh*.")
    assert md("*bold* and [link]") == r"\*bold\* and \[link\]"


def test_pick_reason_explains_a_best_effort_draft():
    llm = FakeLLM(
        analyzer=[brief()], writer=[draft(600)], judge=[failing(), failing("safety", "ending")], reviser=[draft(600)]
    )
    version: StoryVersion = StoryPipeline(llm, max_rounds=2).create(request()).current
    assert "No draft passed every check in 2 review(s), so this shows draft 1" in pick_reason(version)


def test_slider_labels_match_the_control_text():
    for name, table in (("energy", ENERGY), ("silliness", SILLINESS), ("lesson", LESSON)):
        for value, label in CONTROL_LABELS[name].items():
            assert f"({label.lower()})" in table[value].lower()


def test_review_panel_lists_the_must_fix_problems_that_sent_a_draft_back():
    llm = FakeLLM(
        analyzer=[brief()],
        writer=[draft(600)],
        judge=[failing("safety"), verdict(4)],
        reviser=[draft(600, word="cozy")],
    )
    at = tell_story(start(llm))
    panel = "\n".join(m.value for m in at.markdown)
    assert "Draft 1: sent back" in panel and "red-badge[safety]" in panel and "quoted line" in panel
    assert "Draft 2: passed · shown" in panel and "No must-fix problems found" in panel
