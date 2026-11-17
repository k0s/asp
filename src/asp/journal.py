"""The journal: a SQLite record of events, jobs and passes, plus the catalog.

The journal is the record for history and queued work only. The catalog is an
index over the filesystem that the next pass rebuilds, so deleting the database
loses nothing else. Paths are stored as (root, rel), never absolute.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SCHEMA = """
create table passes(
    id integer primary key, started text not null, finished text,
    files integer, events integer, jobs integer);
create table events(
    id integer primary key, ts text not null, pass_id integer,
    source text not null, kind text not null,
    root text not null, rel text not null,
    depth integer not null default 0, size integer, mtime real);
create index events_file on events(root, rel);
create index events_ts on events(ts);
create table catalog(
    root text not null, rel text not null, size integer, mtime real,
    first_seen text not null, last_seen text not null,
    depth integer not null default 0, derived_by text,
    primary key (root, rel)) without rowid;
create table jobs(
    id integer primary key, ts text not null, rule text not null,
    event_id integer not null references events(id),
    state text not null, attempts integer not null default 0,
    output_root text, output_rel text,
    finished text, duration real, error text);
create index jobs_state on jobs(state);
create index jobs_event on jobs(event_id);
create index jobs_output on jobs(output_root, output_rel);
"""

# queued: waiting to run.  done: published its output (or ran, with no output).
# skipped: the handler had nothing to do.  failed: raised, and will be retried.
# parked: out of attempts or over the depth cap; stays until someone retries it.
JOB_STATES = ("queued", "done", "skipped", "failed", "parked")
RUNNABLE = ("queued", "failed")


class JournalError(RuntimeError):
    """The journal can't be opened, or was written by another schema."""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            version = db.execute("pragma user_version").fetchone()[0]
            if version == 0:
                db.execute("pragma journal_mode = wal")
                db.executescript(SCHEMA)
                db.execute(f"pragma user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise JournalError(
                    f"{path}: schema {version}, expected {SCHEMA_VERSION}; "
                    "move it aside (only history and queued work live there)")

    @property
    def files(self) -> frozenset[Path]:
        """The database's own files, which a scan must never report."""
        return frozenset(Path(f"{self.path}{suffix}") for suffix in ("", "-wal", "-shm"))

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A connection whose block is one transaction."""
        try:
            db = sqlite3.connect(self.path, timeout=30)
        except sqlite3.Error as exc:
            raise JournalError(f"{self.path}: {exc}") from exc
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, params)]

    # --- reads, shared by the CLI and the web UI ---

    def summary(self) -> dict[str, Any]:
        since = (datetime.now(UTC) - timedelta(hours=24)).isoformat(timespec="milliseconds")
        last = self.query("select * from passes where finished is not null "
                          "order by id desc limit 1")
        return {
            "roots": self.query(
                "select root, count(*) as files, coalesce(sum(size), 0) as bytes, "
                "sum(derived_by is not null) as derived "
                "from catalog group by root order by root"),
            "events_24h": {r["kind"]: r["n"] for r in self.query(
                "select kind, count(*) as n from events where ts >= ? group by kind",
                (since,))},
            "events_total": self.query("select count(*) as n from events")[0]["n"],
            "jobs": {r["state"]: r["n"] for r in self.query(
                "select state, count(*) as n from jobs group by state")},
            "last_pass": last[0] if last else None,
        }

    def activity(self, hours: int = 48) -> list[dict[str, Any]]:
        """Events per hour and kind over the last `hours`, oldest first; every
        hour is present, so a chart needs no gap-filling."""
        end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(hours=hours - 1)
        counts = {(r["hour"], r["kind"]): r["n"] for r in self.query(
            "select substr(ts, 1, 13) as hour, kind, count(*) as n from events "
            "where ts >= ? group by hour, kind", (start.isoformat(),))}
        buckets = []
        for i in range(hours):
            hour = start + timedelta(hours=i)
            key = hour.isoformat()[:13]
            buckets.append({"hour": hour.isoformat(timespec="minutes"),
                            **{kind: counts.get((key, kind), 0)
                               for kind in ("created", "modified", "deleted")}})
        return buckets

    def events(self, *, kind: str | None = None, root: str | None = None,
               q: str | None = None, before: int | None = None,
               limit: int = 100) -> list[dict[str, Any]]:
        where, params = _where({"kind = ?": kind, "root = ?": root, "id < ?": before})
        if q:
            where.append("instr(rel, ?) > 0")
            params.append(q)
        return self.query(
            f"select * from events {_clause(where)} order by id desc limit ?",
            (*params, limit))

    def jobs(self, *, state: str | None = None, rule: str | None = None,
             before: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        where, params = _where({"j.state = ?": state, "j.rule = ?": rule,
                                "j.id < ?": before})
        return self.query(
            "select j.*, e.root, e.rel, e.kind from jobs j "
            f"join events e on e.id = j.event_id {_clause(where)} "
            "order by j.id desc limit ?", (*params, limit))

    def file(self, root: str, rel: str) -> dict[str, Any]:
        """Everything known about one path: its lineage."""
        entry = self.query("select * from catalog where root = ? and rel = ?", (root, rel))
        return {
            "entry": entry[0] if entry else None,
            "events": self.query("select * from events where root = ? and rel = ? "
                                 "order by id desc limit 200", (root, rel)),
            "jobs": self.query(
                "select j.*, e.root, e.rel, e.kind from jobs j "
                "join events e on e.id = j.event_id "
                "where e.root = ? and e.rel = ? order by j.id desc limit 200", (root, rel)),
            "made_by": self.query(
                "select j.*, e.root, e.rel, e.kind from jobs j "
                "join events e on e.id = j.event_id "
                "where j.output_root = ? and j.output_rel = ? and j.state = 'done' "
                "order by j.id desc limit 20", (root, rel)),
        }

    def catalog(self, *, root: str | None = None, q: str | None = None,
                derived: bool | None = None, limit: int = 200) -> list[dict[str, Any]]:
        where, params = _where({"root = ?": root})
        if q:
            where.append("instr(rel, ?) > 0")
            params.append(q)
        if derived is not None:
            where.append("derived_by is not null" if derived else "derived_by is null")
        return self.query(
            f"select * from catalog {_clause(where)} order by last_seen desc, rel limit ?",
            (*params, limit))

    def passes(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.query("select * from passes order by id desc limit ?", (limit,))

    # --- writes outside a pass ---

    def retry(self, job_id: int) -> bool:
        """Put a parked or failed job back in the queue with a clean slate."""
        with self.connect() as db:
            done = db.execute(
                "update jobs set state = 'queued', attempts = 0, error = null "
                "where id = ? and state in ('parked', 'failed')", (job_id,))
            return done.rowcount == 1


def _where(conditions: dict[str, Any]) -> tuple[list[str], list[Any]]:
    kept = {sql: value for sql, value in conditions.items() if value not in (None, "")}
    return list(kept), list(kept.values())


def _clause(where: list[str]) -> str:
    return "where " + " and ".join(where) if where else ""
