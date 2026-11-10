import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
create table if not exists app_user(
  id text primary key, name text not null unique, password_hash text not null);

create table if not exists meeting(
  id text primary key, kind text not null check(kind in ('general','board')),
  title text not null, held_on text not null, starts_at text not null, ended_at text,
  place text not null, state text not null default 'prepared', assembly_ref text,
  attendance_confirmed_at text, current_item_id text);

create table if not exists agenda_item(
  id text primary key, meeting_id text not null references meeting(id),
  position integer not null, title text not null,
  is_vote integer not null default 0, no_record integer not null default 0, assembly_item_ref text);

create table if not exists participant(
  id text primary key, meeting_id text not null references meeting(id),
  name text not null, member_ref text, is_guest integer not null default 0,
  arrived_at text not null, left_at text);

create table if not exists consent_text(
  sha256 text primary key, body text not null, created_at text not null);

create table if not exists consent_event(
  id text primary key, participant_id text not null references participant(id),
  type text not null check(type in ('yes','no','revoked','attested')),
  at text not null, recorded_by text not null,
  text_sha256 text not null references consent_text(sha256));

create table if not exists recording(
  id text primary key, meeting_id text not null references meeting(id),
  started_at text not null, stopped_at text, deleted_at text,
  source text not null check(source in ('browser','upload')));

create table if not exists chunk(
  recording_id text not null references recording(id), seq integer not null,
  received_at text not null, primary key(recording_id, seq));

create table if not exists segment(
  id text primary key, recording_id text not null references recording(id),
  start_ms integer not null, end_ms integer not null, speaker_label text, text text not null);

create table if not exists proposal(
  id text primary key, meeting_id text not null references meeting(id),
  agenda_item_id text references agenda_item(id),
  kind text not null check(kind in ('summary','resolution','task')),
  payload_json text not null, source_segment_ids text not null,
  status text not null default 'pending' check(status in ('pending','accepted','rejected')));

create table if not exists resolution(
  id text primary key, number text not null unique, meeting_id text not null references meeting(id),
  text text not null, yes integer, no integer, abstain integer,
  supersedes_id text references resolution(id), approved_at text not null, approved_by text not null);

create table if not exists job(
  id text primary key, meeting_id text not null references meeting(id), step text not null,
  status text not null default 'queued' check(status in ('queued','running','done','failed')),
  attempts integer not null default 0, error text);

create table if not exists handover(
  id text primary key, meeting_id text not null references meeting(id), at text not null,
  actor text not null, entity text not null, local_ref text not null, remote_id text,
  status text not null, unique(meeting_id, entity, local_ref));

create table if not exists audit(
  id integer primary key autoincrement, at text not null, actor text not null,
  action text not null, entity text not null, entity_id text not null);
"""

APPEND_ONLY = ("consent_text", "consent_event", "resolution", "audit")


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    conn.execute("pragma journal_mode = wal")
    # secure_delete overwrites freed pages, so purged transcripts do not linger in the file.
    conn.execute("pragma secure_delete = on")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table in APPEND_ONLY:
        for op in ("update", "delete"):
            conn.execute(
                f"create trigger if not exists {table}_no_{op} before {op} on {table} "
                f"begin select raise(abort, '{table} is append-only'); end"
            )


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("begin immediate")
    try:
        yield conn
    except BaseException:
        conn.execute("rollback")
        raise
    conn.execute("commit")
