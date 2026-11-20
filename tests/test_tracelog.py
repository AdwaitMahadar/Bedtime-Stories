"""Trace logs: saved as JSON Lines with call metadata only, and rendered as a timeline."""

from storyteller import tracelog
from storyteller.models import ChildProfile, StoryRequest
from storyteller.pipeline import StoryPipeline

from .fakes import FakeLLM, brief, draft, verdict


def _session():
    llm = FakeLLM(analyzer=[brief()], writer=[draft(600, title="Mia and Moonpenny")], judge=[verdict(4)])
    request = StoryRequest(text="A story for Mia", profile=ChildProfile(name="Mia", age=6, avoid=["spiders"]))
    return StoryPipeline(llm).create(request)


def test_save_writes_one_line_per_call_and_loads_back(tmp_path):
    session = _session()
    path = tracelog.save(session, tmp_path)
    assert path.parent == tmp_path and path.name.endswith(f"{session.id}.jsonl")
    assert len(path.read_text().splitlines()) == 3
    assert [e.step for e in tracelog.load(path)] == ["analyzer", "writer", "judge"]


def test_logs_hold_no_story_text_or_child_details(tmp_path):
    text = tracelog.save(_session(), tmp_path).read_text()
    for private in ("Moonpenny", "Mia", "spiders", "A story for"):
        assert private not in text


def test_save_never_raises_when_the_folder_cannot_be_written(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a folder")
    assert tracelog.save(_session(), blocked) is None


def test_timeline_lists_every_call_and_totals():
    table = tracelog.timeline(_session().trace)
    assert "analyzer" in table and "writer" in table and "judge" in table
    assert "total" in table.splitlines()[-1]
