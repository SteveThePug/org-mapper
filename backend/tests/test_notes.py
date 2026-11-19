"""Colleagues' notes reach the AI brief as citable facts, unless the author opted out."""

from datetime import UTC, datetime

from conftest import make_person

from radar.config import AppConfig
from radar.llm.tasks import brief_facts, note_fact_id
from radar.models import Note


def make_note(note_id: str, text: str, use_in_brief: bool = True) -> Note:
    return Note(
        id=note_id,
        person_id="p1",
        text=text,
        author="rec@x.test",
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
        use_in_brief=use_in_brief,
    )


def test_notes_become_facts_without_the_author(app_config: AppConfig) -> None:
    shared = make_note("aaaaaaaa1111", "Open to a move in spring")
    private = make_note("bbbbbbbb2222", "Personal matter", use_in_brief=False)
    facts = brief_facts(make_person(), app_config, [], [], [shared, private])
    notes = [f for f in facts if f.id.startswith("N-")]
    assert [f.id for f in notes] == [note_fact_id(shared)] == ["N-aaaaaaaa"]
    assert "Open to a move in spring" in notes[0].text and "rec@x.test" not in notes[0].text
