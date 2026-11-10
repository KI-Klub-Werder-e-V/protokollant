import sqlite3

import pytest

from protokollant import meetings as m
from protokollant.db import connect, migrate

TEXT = "Einwilligungstext v1"
ACTOR = "protokoll"


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def meeting(conn, agenda=None):
    return m.create_meeting(
        conn, ACTOR, kind="board", title="Sitzung", held_on="2026-11-09", starts_at="19:00",
        place="Vereinsheim", agenda=agenda or [{"title": "Bericht"}],
    )


def ready(conn, names=("Anna", "Bert")):
    mid = meeting(conn)
    people = [m.add_participant(conn, ACTOR, mid, n) for n in names]
    for p in people:
        m.record_consent(conn, ACTOR, p, "yes", TEXT)
    m.confirm_attendance(conn, ACTOR, mid)
    return mid, people


def state(conn, mid):
    return conn.execute("select state from meeting where id=?", (mid,)).fetchone()[0]


def test_one_missing_consent_blocks_start(conn):
    mid = meeting(conn)
    a = m.add_participant(conn, ACTOR, mid, "Anna")
    m.add_participant(conn, ACTOR, mid, "Bert")
    m.record_consent(conn, ACTOR, a, "yes", TEXT)
    m.confirm_attendance(conn, ACTOR, mid)
    with pytest.raises(m.ConsentMissing):
        m.start_recording(conn, ACTOR, mid)
    assert conn.execute("select count(*) from recording").fetchone()[0] == 0


def test_unconfirmed_list_and_empty_list_block_start(conn):
    mid = meeting(conn)
    m.confirm_attendance(conn, ACTOR, mid)
    with pytest.raises(m.ConsentMissing):
        m.start_recording(conn, ACTOR, mid)
    mid2 = meeting(conn)
    p = m.add_participant(conn, ACTOR, mid2, "Anna")
    m.record_consent(conn, ACTOR, p, "yes", TEXT)
    with pytest.raises(m.ConsentMissing):
        m.start_recording(conn, ACTOR, mid2)


def test_all_yes_starts(conn):
    mid, _ = ready(conn)
    m.start_recording(conn, ACTOR, mid)
    assert state(conn, mid) == "recording"


def test_guest_needs_consent_too(conn):
    mid, _ = ready(conn)
    m.add_participant(conn, ACTOR, mid, "Gast", is_guest=True)
    m.confirm_attendance(conn, ACTOR, mid)
    with pytest.raises(m.ConsentMissing):
        m.start_recording(conn, ACTOR, mid)


def test_late_arrival_pauses_and_blocks_until_consent(conn, tmp_path):
    mid, _ = ready(conn)
    rec = m.start_recording(conn, ACTOR, mid)
    late = m.add_participant(conn, ACTOR, mid, "Nachzüglerin")
    assert state(conn, mid) == "paused"
    with pytest.raises(m.Conflict):
        m.accept_chunk(conn, tmp_path, rec, 0, b"x")
    m.confirm_attendance(conn, ACTOR, mid)
    with pytest.raises(m.ConsentMissing):
        m.start_recording(conn, ACTOR, mid)
    m.record_consent(conn, ACTOR, late, "yes", TEXT)
    m.start_recording(conn, ACTOR, mid)
    assert state(conn, mid) == "recording"


def test_late_no_blocks_resume_but_keeps_earlier_audio_processable(conn, tmp_path):
    mid, _ = ready(conn)
    rec = m.start_recording(conn, ACTOR, mid)
    m.accept_chunk(conn, tmp_path, rec, 0, b"x")
    late = m.add_participant(conn, ACTOR, mid, "Nein")
    m.record_consent(conn, ACTOR, late, "no", TEXT)
    m.confirm_attendance(conn, ACTOR, mid)
    with pytest.raises(m.ConsentMissing):
        m.start_recording(conn, ACTOR, mid)
    m.stop_recording(conn, ACTOR, mid)
    assert state(conn, mid) == "processing"


def test_no_before_recording_goes_to_no_recording(conn):
    mid = meeting(conn)
    p = m.add_participant(conn, ACTOR, mid, "Anna")
    m.record_consent(conn, ACTOR, p, "no", TEXT)
    assert state(conn, mid) == "no_recording"
    with pytest.raises(m.Conflict):
        m.start_recording(conn, ACTOR, mid)


def test_summary_never_names_people(conn):
    mid = meeting(conn)
    p = m.add_participant(conn, ACTOR, mid, "Anna")
    m.add_participant(conn, ACTOR, mid, "Bert")
    m.record_consent(conn, ACTOR, p, "yes", TEXT)
    assert m.consent_summary(conn, mid) == {"present": 2, "yes": 1, "open": 1, "blocked": False}


def test_person_who_left_no_longer_blocks(conn):
    mid = meeting(conn)
    a = m.add_participant(conn, ACTOR, mid, "Anna")
    b = m.add_participant(conn, ACTOR, mid, "Bert")
    m.record_consent(conn, ACTOR, a, "yes", TEXT)
    m.participant_left(conn, ACTOR, b)
    m.confirm_attendance(conn, ACTOR, mid)
    m.start_recording(conn, ACTOR, mid)
    assert state(conn, mid) == "recording"


def test_no_record_item_pauses_and_blocks_resume(conn):
    mid = meeting(conn, [{"title": "Offen"}, {"title": "Personal", "no_record": True}])
    p = m.add_participant(conn, ACTOR, mid, "Anna")
    m.record_consent(conn, ACTOR, p, "yes", TEXT)
    m.confirm_attendance(conn, ACTOR, mid)
    m.start_recording(conn, ACTOR, mid)
    items = conn.execute("select id from agenda_item order by position").fetchall()
    m.enter_agenda_item(conn, ACTOR, mid, items[1]["id"])
    assert state(conn, mid) == "paused"
    with pytest.raises(m.ConsentMissing):
        m.start_recording(conn, ACTOR, mid)
    m.enter_agenda_item(conn, ACTOR, mid, items[0]["id"])
    m.start_recording(conn, ACTOR, mid)
    assert state(conn, mid) == "recording"


def test_revoke_all_deletes_audio_and_transcript_and_keeps_evidence(conn, tmp_path):
    mid, people = ready(conn)
    rec = m.start_recording(conn, ACTOR, mid)
    m.accept_chunk(conn, tmp_path, rec, 0, b"audio")
    conn.execute("insert into segment(id,recording_id,start_ms,end_ms,text) values('s',?,0,1,'x')", (rec,))
    path = m.chunk_path(tmp_path, mid, rec, 0)
    assert path.exists()
    m.revoke(conn, tmp_path, ACTOR, people[0], TEXT, delete_all=True)
    assert state(conn, mid) == "paused"
    assert not path.exists()
    assert conn.execute("select count(*) from segment").fetchone()[0] == 0
    assert conn.execute("select deleted_at from recording").fetchone()[0]
    assert conn.execute("select count(*) from consent_event where type='revoked'").fetchone()[0] == 1
    with pytest.raises(m.ConsentMissing):
        m.start_recording(conn, ACTOR, mid)


def test_revoke_from_time_keeps_earlier_chunks(conn, tmp_path):
    mid, people = ready(conn)
    rec = m.start_recording(conn, ACTOR, mid)
    m.accept_chunk(conn, tmp_path, rec, 0, b"early")
    m.accept_chunk(conn, tmp_path, rec, 1, b"late")
    cutoff = conn.execute("select received_at from chunk where seq=1").fetchone()[0]
    m.revoke(conn, tmp_path, ACTOR, people[0], TEXT, delete_from=cutoff)
    assert m.chunk_path(tmp_path, mid, rec, 0).exists()
    assert not m.chunk_path(tmp_path, mid, rec, 1).exists()


def test_upload_requires_attestation(conn):
    mid = meeting(conn)
    m.add_participant(conn, ACTOR, mid, "Anna")
    m.confirm_attendance(conn, ACTOR, mid)
    with pytest.raises(m.ConsentMissing):
        m.attest_upload(conn, ACTOR, mid, attested=False, consent_text=TEXT)
    m.attest_upload(conn, ACTOR, mid, attested=True, consent_text=TEXT)
    assert conn.execute("select type from consent_event").fetchone()[0] == "attested"


def test_attested_never_unlocks_live_recording(conn):
    mid = meeting(conn)
    m.add_participant(conn, ACTOR, mid, "Anna")
    m.confirm_attendance(conn, ACTOR, mid)
    m.attest_upload(conn, ACTOR, mid, attested=True, consent_text=TEXT)
    with pytest.raises(m.Conflict):
        m.start_recording(conn, ACTOR, mid)


def test_stop_queues_exactly_one_job(conn):
    mid, _ = ready(conn)
    m.start_recording(conn, ACTOR, mid)
    m.stop_recording(conn, ACTOR, mid)
    assert conn.execute("select count(*) from job where status='queued'").fetchone()[0] == 1
    with pytest.raises(m.Conflict):
        m.stop_recording(conn, ACTOR, mid)


def test_purge_removes_all_media(conn, tmp_path):
    mid, _ = ready(conn)
    rec = m.start_recording(conn, ACTOR, mid)
    m.accept_chunk(conn, tmp_path, rec, 0, b"audio")
    m.stop_recording(conn, ACTOR, mid)
    (tmp_path / "work" / mid).mkdir(parents=True)
    (tmp_path / "work" / mid / "n.wav").write_bytes(b"x")
    m.purge_media(conn, tmp_path, ACTOR, mid)
    assert not (tmp_path / "audio" / mid).exists()
    assert not (tmp_path / "work" / mid).exists()


@pytest.mark.parametrize("table,where", [("consent_event", "1=1"), ("audit", "1=1")])
@pytest.mark.parametrize("sql", ["delete from {t} where {w}", "update {t} set at='x' where {w}"])
def test_evidence_is_append_only(conn, table, where, sql):
    ready(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(sql.format(t=table, w=where))
