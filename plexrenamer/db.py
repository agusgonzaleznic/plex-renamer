"""SQLite journal. Every op is recorded before it happens and updated after."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS run (
    id           INTEGER PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    root         TEXT NOT NULL,
    categories   TEXT NOT NULL,
    plan_hash    TEXT,
    status       TEXT NOT NULL,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS op (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES run(id),
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    src         TEXT,
    dst         TEXT,
    is_dir      INTEGER NOT NULL DEFAULT 0,
    size        INTEGER,
    inode       INTEGER,
    category    TEXT,
    reason      TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    error       TEXT,
    applied_at  TEXT,
    undone_at   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS op_run_seq ON op(run_id, seq);
CREATE INDEX IF NOT EXISTS op_status ON op(run_id, status);

CREATE TABLE IF NOT EXISTS idcache (
    key        TEXT PRIMARY KEY,
    provider   TEXT,
    value      TEXT,
    title      TEXT,
    year       INTEGER,
    source     TEXT,
    created_at TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def create_run(conn, root: str, categories, plan_hash: str, note: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO run(started_at, root, categories, plan_hash, status, note)"
        " VALUES (?,?,?,?,?,?)",
        (utcnow(), root, ",".join(categories), plan_hash, "planned", note),
    )
    conn.commit()
    return cur.lastrowid


def insert_ops(conn, run_id: int, ops) -> None:
    conn.executemany(
        "INSERT INTO op(run_id, seq, kind, src, dst, is_dir, size, inode, category, reason)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (run_id, o.seq, o.kind, o.src, o.dst, int(o.is_dir), o.size, o.inode,
             o.category, o.reason)
            for o in ops
        ],
    )
    conn.commit()


def append_op(conn, run_id: int, kind: str, src, dst, *, size: int = 0, inode: int = 0,
              reason: str = "", status: str = "pending") -> int:
    """Add one op behind the planned ones, for a move that is only known at run time.

    It gets the highest seq of its run, so undo reverses it before anything that
    was planned. The caller commits.
    """
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM op WHERE run_id=?",
                       (run_id,)).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO op(run_id, seq, kind, src, dst, is_dir, size, inode, category,"
        " reason, status, applied_at) VALUES (?,?,?,?,?,0,?,?,'',?,?,?)",
        (run_id, seq, kind, src, dst, size, inode, reason, status, utcnow()),
    )
    return cur.lastrowid


def note_op(conn, op_id: int, note: str) -> None:
    """Record why an op could not be undone. Its status stays, so the next undo
    tries it again."""
    conn.execute("UPDATE op SET error=?, undone_at=? WHERE id=?",
                 (note, utcnow(), op_id))


def get_run(conn, run_id: int):
    return conn.execute("SELECT * FROM run WHERE id=?", (run_id,)).fetchone()


# A run still stands when it may have changed the disk and is not fully undone.
_STANDING = "status NOT IN ('undone', 'planned')"


def newest_standing_run(conn):
    row = conn.execute(
        f"SELECT id FROM run WHERE {_STANDING} ORDER BY id DESC LIMIT 1").fetchone()
    return row["id"] if row else None


def standing_runs_after(conn, run_id: int, root: str):
    """Later runs on the same library that are not undone, newest first."""
    same = os.path.normpath(root)        # "/Volumes/X/" and "/Volumes/X" are one library
    return [row["id"] for row in conn.execute(
        f"SELECT id, root FROM run WHERE id > ? AND {_STANDING} ORDER BY id DESC", (run_id,))
        if os.path.normpath(row["root"]) == same]


def set_run_status(conn, run_id: int, status: str, finished: bool = False) -> None:
    if finished:
        conn.execute("UPDATE run SET status=?, finished_at=? WHERE id=?",
                     (status, utcnow(), run_id))
    else:
        conn.execute("UPDATE run SET status=? WHERE id=?", (status, run_id))
    conn.commit()


def mark_op(conn, op_id: int, status: str, error: str | None = None,
            undone: bool = False) -> None:
    field = "undone_at" if undone else "applied_at"
    conn.execute(
        f"UPDATE op SET status=?, error=?, {field}=? WHERE id=?",
        (status, error, utcnow(), op_id),
    )


def latest_run(conn, status=None):
    if status:
        row = conn.execute(
            "SELECT * FROM run WHERE status=? ORDER BY id DESC LIMIT 1", (status,)
        ).fetchone()
    else:
        row = conn.execute("SELECT * FROM run ORDER BY id DESC LIMIT 1").fetchone()
    return row


def run_ops(conn, run_id: int, reverse: bool = False):
    order = "DESC" if reverse else "ASC"
    return conn.execute(
        f"SELECT * FROM op WHERE run_id=? ORDER BY seq {order}", (run_id,)
    ).fetchall()
