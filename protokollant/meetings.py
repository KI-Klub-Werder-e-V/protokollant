"""The meeting state machine. UI, API and MCP all go through here, so the consent lock has one home."""

import hashlib
import shutil
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .db import tx


class Conflict(Exception):
    """The action is not allowed in the meeting's current state (HTTP 409)."""


class ConsentMissing(Conflict):
    pass


CONSENT_STATES = ("prepared", "consent", "ready", "paused")
_no_more_people = ("processing", "error", "review", "approved", "handed_over")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _id() -> str:
    return str(uuid.uuid4())


def _audit(conn: sqlite3.Connection, actor: str, action: str, entity: str, entity_id: str) -> None:
    conn.execute(
        "insert into audit(at, actor, action, entity, entity_id) values (?,?,?,?,?)",
        (_now(), actor, action, entity, entity_id),
    )


def _meeting(conn: sqlite3.Connection, meeting_id: str) -> sqlite3.Row:
    row = conn.execute("select * from meeting where id = ?", (meeting_id,)).fetchone()
    if row is None:
        raise KeyError(meeting_id)
    return row


def _set_state(conn: sqlite3.Connection, meeting_id: str, state: str) -> None:
    conn.execute("update meeting set state = ? where id = ?", (state, meeting_id))


def _open_recording(conn: sqlite3.Connection, meeting_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "select * from recording where meeting_id = ? and stopped_at is null and deleted_at is null",
        (meeting_id,),
    ).fetchone()


def _close_recording(conn: sqlite3.Connection, meeting_id: str) -> None:
    conn.execute(
        "update recording set stopped_at = ? where meeting_id = ? and stopped_at is null",
        (_now(), meeting_id),
    )


def _has_audio(conn: sqlite3.Connection, meeting_id: str) -> bool:
    return (
        conn.execute(
            "select 1 from recording where meeting_id = ? and deleted_at is null", (meeting_id,)
        ).fetchone()
        is not None
    )


def _present(conn: sqlite3.Connection, meeting_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "select * from participant where meeting_id = ? and left_at is null", (meeting_id,)
    ).fetchall()


def _latest_consent(conn: sqlite3.Connection, participant_id: str) -> str | None:
    # rowid, not the timestamp: two events can share a clock tick and the last one must win.
    row = conn.execute(
        "select type from consent_event where participant_id = ? order by rowid desc limit 1",
        (participant_id,),
    ).fetchone()
    return row["type"] if row else None


def _store_text(conn: sqlite3.Connection, body: str) -> str:
    digest = hashlib.sha256(body.encode()).hexdigest()
    conn.execute(
        "insert or ignore into consent_text(sha256, body, created_at) values (?,?,?)",
        (digest, body, _now()),
    )
    return digest


def _event(
    conn: sqlite3.Connection, participant_id: str, kind: str, actor: str, digest: str
) -> None:
    conn.execute(
        "insert into consent_event(id, participant_id, type, at, recorded_by, text_sha256) "
        "values (?,?,?,?,?,?)",
        (_id(), participant_id, kind, _now(), actor, digest),
    )


def _assert_may_record(conn: sqlite3.Connection, m: sqlite3.Row) -> None:
    """E1: the lock. Raises unless every person present has said yes and the list is confirmed."""
    if not m["attendance_confirmed_at"]:
        raise ConsentMissing("Die Liste der Anwesenden ist nicht bestätigt.")
    present = _present(conn, m["id"])
    if not present:
        raise ConsentMissing("Es sind keine Anwesenden eingetragen.")
    if any(_latest_consent(conn, p["id"]) != "yes" for p in present):
        raise ConsentMissing("Es haben noch nicht alle Anwesenden eingewilligt.")
    if m["current_item_id"]:
        item = conn.execute(
            "select no_record from agenda_item where id = ?", (m["current_item_id"],)
        ).fetchone()
        if item and item["no_record"]:
            raise ConsentMissing("Dieser Tagesordnungspunkt wird nicht aufgenommen.")


def create_meeting(
    conn: sqlite3.Connection,
    actor: str,
    *,
    kind: str,
    title: str,
    held_on: str,
    starts_at: str,
    place: str,
    agenda: list[dict],
) -> str:
    meeting_id = _id()
    with tx(conn):
        conn.execute(
            "insert into meeting(id, kind, title, held_on, starts_at, place) values (?,?,?,?,?,?)",
            (meeting_id, kind, title, held_on, starts_at, place),
        )
        for position, item in enumerate(agenda, start=1):
            conn.execute(
                "insert into agenda_item(id, meeting_id, position, title, is_vote, no_record) "
                "values (?,?,?,?,?,?)",
                (
                    _id(),
                    meeting_id,
                    position,
                    item["title"],
                    int(item.get("is_vote", False)),
                    int(item.get("no_record", False)),
                ),
            )
        _audit(conn, actor, "meeting.created", "meeting", meeting_id)
    return meeting_id


def add_participant(
    conn: sqlite3.Connection,
    actor: str,
    meeting_id: str,
    name: str,
    *,
    is_guest: bool = False,
    member_ref: str | None = None,
) -> str:
    """E5, E7: anyone can be added; adding someone mid-recording pauses it until they have answered."""
    participant_id = _id()
    with tx(conn):
        m = _meeting(conn, meeting_id)
        if m["state"] in _no_more_people:
            raise Conflict("Die Sitzung ist beendet.")
        conn.execute(
            "insert into participant(id, meeting_id, name, member_ref, is_guest, arrived_at) "
            "values (?,?,?,?,?,?)",
            (participant_id, meeting_id, name, member_ref, int(is_guest), _now()),
        )
        conn.execute(
            "update meeting set attendance_confirmed_at = null where id = ?", (meeting_id,)
        )
        if m["state"] == "recording":
            _close_recording(conn, meeting_id)
            _set_state(conn, meeting_id, "paused")
            _audit(conn, actor, "recording.paused_new_person", "meeting", meeting_id)
        elif m["state"] == "ready":
            _set_state(conn, meeting_id, "consent")
        _audit(conn, actor, "participant.added", "participant", participant_id)
    return participant_id


def participant_left(conn: sqlite3.Connection, actor: str, participant_id: str) -> None:
    with tx(conn):
        conn.execute("update participant set left_at = ? where id = ?", (_now(), participant_id))
        _audit(conn, actor, "participant.left", "participant", participant_id)


def confirm_attendance(conn: sqlite3.Connection, actor: str, meeting_id: str) -> None:
    with tx(conn):
        _meeting(conn, meeting_id)
        conn.execute(
            "update meeting set attendance_confirmed_at = ? where id = ?", (_now(), meeting_id)
        )
        _audit(conn, actor, "attendance.confirmed", "meeting", meeting_id)


def record_consent(
    conn: sqlite3.Connection, actor: str, participant_id: str, answer: str, consent_text: str
) -> None:
    """E2, E3, E6. One person, one answer. A no ends recording for this meeting's remaining time."""
    if answer not in ("yes", "no"):
        raise ValueError(answer)
    with tx(conn):
        p = conn.execute("select * from participant where id = ?", (participant_id,)).fetchone()
        if p is None:
            raise KeyError(participant_id)
        m = _meeting(conn, p["meeting_id"])
        if m["state"] not in CONSENT_STATES:
            raise Conflict("Einwilligungen werden vor der Aufnahme erfasst.")
        _event(conn, participant_id, answer, actor, _store_text(conn, consent_text))
        if answer == "no":
            if not _has_audio(conn, m["id"]):
                _set_state(conn, m["id"], "no_recording")
        elif m["state"] == "prepared":
            _set_state(conn, m["id"], "consent")
        _audit(conn, actor, f"consent.{answer}", "participant", participant_id)


def consent_summary(conn: sqlite3.Connection, meeting_id: str) -> dict:
    """E6: counts only. Shared screens must not single out who said no."""
    answers = [_latest_consent(conn, p["id"]) for p in _present(conn, meeting_id)]
    return {
        "present": len(answers),
        "yes": answers.count("yes"),
        "open": answers.count(None),
        "blocked": sum(a in ("no", "revoked") for a in answers) > 0,
    }


def start_recording(conn: sqlite3.Connection, actor: str, meeting_id: str) -> str:
    """Starts or resumes. Every resume is a new recording row, so paused stretches contain no audio."""
    recording_id = _id()
    with tx(conn):
        m = _meeting(conn, meeting_id)
        if m["state"] not in ("consent", "ready", "paused", "prepared"):
            raise Conflict("Die Aufnahme kann in diesem Zustand nicht starten.")
        _assert_may_record(conn, m)
        conn.execute(
            "insert into recording(id, meeting_id, started_at, source) values (?,?,?,'browser')",
            (recording_id, meeting_id, _now()),
        )
        _set_state(conn, meeting_id, "recording")
        _audit(conn, actor, "recording.started", "meeting", meeting_id)
    return recording_id


def pause_recording(conn: sqlite3.Connection, actor: str, meeting_id: str) -> None:
    with tx(conn):
        if _meeting(conn, meeting_id)["state"] != "recording":
            raise Conflict("Es läuft keine Aufnahme.")
        _close_recording(conn, meeting_id)
        _set_state(conn, meeting_id, "paused")
        _audit(conn, actor, "recording.paused", "meeting", meeting_id)


def enter_agenda_item(conn: sqlite3.Connection, actor: str, meeting_id: str, item_id: str) -> None:
    """E9: calling up a no-record item stops the tape before anyone speaks to it."""
    with tx(conn):
        m = _meeting(conn, meeting_id)
        item = conn.execute(
            "select * from agenda_item where id = ? and meeting_id = ?", (item_id, meeting_id)
        ).fetchone()
        if item is None:
            raise KeyError(item_id)
        conn.execute("update meeting set current_item_id = ? where id = ?", (item_id, meeting_id))
        if item["no_record"] and m["state"] == "recording":
            _close_recording(conn, meeting_id)
            _set_state(conn, meeting_id, "paused")
            _audit(conn, actor, "recording.paused_no_record_item", "meeting", meeting_id)


def accept_chunk(
    conn: sqlite3.Connection, data_dir: Path, recording_id: str, seq: int, data: bytes
) -> None:
    """Refuses bytes unless this recording is open right now: a paused tape cannot receive audio."""
    with tx(conn):
        rec = conn.execute("select * from recording where id = ?", (recording_id,)).fetchone()
        if rec is None:
            raise KeyError(recording_id)
        if rec["stopped_at"] or rec["deleted_at"]:
            raise Conflict("Diese Aufnahme ist nicht mehr offen.")
        if _meeting(conn, rec["meeting_id"])["state"] != "recording":
            raise Conflict("Es läuft keine Aufnahme.")
        conn.execute(
            "insert into chunk(recording_id, seq, received_at) values (?,?,?)",
            (recording_id, seq, _now()),
        )
        path = chunk_path(data_dir, rec["meeting_id"], recording_id, seq)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def chunk_path(data_dir: Path, meeting_id: str, recording_id: str, seq: int) -> Path:
    return data_dir / "audio" / meeting_id / recording_id / f"{seq:08d}.part"


def stop_recording(conn: sqlite3.Connection, actor: str, meeting_id: str) -> None:
    """Ends the meeting's recording phase and queues the processing job."""
    with tx(conn):
        m = _meeting(conn, meeting_id)
        if m["state"] not in ("recording", "paused"):
            raise Conflict("Es gibt keine Aufnahme zu beenden.")
        _close_recording(conn, meeting_id)
        conn.execute("update meeting set ended_at = ? where id = ?", (_now(), meeting_id))
        if _has_audio(conn, meeting_id):
            _set_state(conn, meeting_id, "processing")
            conn.execute(
                "insert into job(id, meeting_id, step) values (?,?,'process')", (_id(), meeting_id)
            )
        else:
            _set_state(conn, meeting_id, "no_recording")
        _audit(conn, actor, "recording.stopped", "meeting", meeting_id)


def attest_upload(
    conn: sqlite3.Connection, actor: str, meeting_id: str, *, attested: bool, consent_text: str
) -> str:
    """E10: no statement, no upload. Bytes are only accepted against the recording this returns."""
    if not attested:
        raise ConsentMissing("Ohne Erklärung zur Einwilligung ist kein Upload möglich.")
    recording_id = _id()
    with tx(conn):
        m = _meeting(conn, meeting_id)
        if m["state"] not in ("prepared", "consent"):
            raise Conflict("Ein Upload ist in diesem Zustand nicht möglich.")
        if not m["attendance_confirmed_at"]:
            raise ConsentMissing("Die Liste der Anwesenden ist nicht bestätigt.")
        present = _present(conn, meeting_id)
        if not present:
            raise ConsentMissing("Es sind keine Anwesenden eingetragen.")
        digest = _store_text(conn, consent_text)
        for p in present:
            _event(conn, p["id"], "attested", actor, digest)
        conn.execute(
            "insert into recording(id, meeting_id, started_at, source) values (?,?,?,'upload')",
            (recording_id, meeting_id, _now()),
        )
        _set_state(conn, meeting_id, "recording")
        _audit(conn, actor, "upload.attested", "meeting", meeting_id)
    return recording_id


def revoke(
    conn: sqlite3.Connection,
    data_dir: Path,
    actor: str,
    participant_id: str,
    consent_text: str,
    *,
    delete_from: str | None = None,
    delete_all: bool = False,
) -> None:
    """E8: always accepted, in any state. Stops the tape, then deletes what the person asked for."""
    with tx(conn):
        p = conn.execute("select * from participant where id = ?", (participant_id,)).fetchone()
        if p is None:
            raise KeyError(participant_id)
        meeting_id = p["meeting_id"]
        m = _meeting(conn, meeting_id)
        _event(conn, participant_id, "revoked", actor, _store_text(conn, consent_text))
        if m["state"] == "recording":
            _close_recording(conn, meeting_id)
            _set_state(conn, meeting_id, "paused")
        doomed: list[Path] = []
        if delete_all:
            doomed = _delete_after(conn, data_dir, meeting_id, None)
        elif delete_from:
            doomed = _delete_after(conn, data_dir, meeting_id, delete_from)
        _audit(conn, actor, "consent.revoked", "participant", participant_id)
    for path in doomed:
        path.unlink(missing_ok=True)


def _delete_after(
    conn: sqlite3.Connection, data_dir: Path, meeting_id: str, cutoff: str | None
) -> list[Path]:
    """Removes the rows and returns the files, so files only go once the transaction is certain."""
    doomed: list[Path] = []
    for rec in conn.execute(
        "select * from recording where meeting_id = ? and deleted_at is null", (meeting_id,)
    ).fetchall():
        chunks = conn.execute(
            "select seq, received_at from chunk where recording_id = ?", (rec["id"],)
        ).fetchall()
        gone = [c for c in chunks if cutoff is None or c["received_at"] >= cutoff]
        doomed += [chunk_path(data_dir, meeting_id, rec["id"], c["seq"]) for c in gone]
        conn.executemany(
            "delete from chunk where recording_id = ? and seq = ?",
            [(rec["id"], c["seq"]) for c in gone],
        )
        # Offsets shift once audio is cut, so a recording's transcript cannot be partly trusted.
        if gone or cutoff is None:
            conn.execute("delete from segment where recording_id = ?", (rec["id"],))
        if len(gone) == len(chunks):
            conn.execute("update recording set deleted_at = ? where id = ?", (_now(), rec["id"]))
    return doomed


def purge_media(conn: sqlite3.Connection, data_dir: Path, actor: str, meeting_id: str) -> None:
    """Deletes audio, transcript and work files. Runs at approval and from the retention job."""
    with tx(conn):
        _delete_after(conn, data_dir, meeting_id, None)
        _audit(conn, actor, "media.purged", "meeting", meeting_id)
    shutil.rmtree(data_dir / "audio" / meeting_id, ignore_errors=True)
    shutil.rmtree(data_dir / "work" / meeting_id, ignore_errors=True)
