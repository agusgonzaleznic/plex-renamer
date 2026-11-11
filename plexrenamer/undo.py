"""Reverse a run.

Undo is built on two things that survive whatever stopped a run: the plan, which
is in the journal before the first operation, and the disk. The status of a row
only says what to expect. Each row is settled by where the file it names is now,
by inode where one was recorded, so undo never moves a file it cannot identify.

A row that cannot be settled keeps its status and is tried again by the next
undo. Nothing is marked as undone that is not.
"""

from __future__ import annotations

import os

from . import db
from .executor import METADATA_RE, PlanError, Progress, free_name, holds

# What the journal says about a row before undo looks at the disk. An expected
# row ran, or was in flight when its run was cut off ("started"), so it has to be
# accounted for. An unproven row reported an error ("failed") or was never marked
# ("pending"): it is reversed only when the disk shows that it happened after all,
# which is what a lost reply from a network share or a lost journal mark looks like.
EXPECTED = ("done", "started", "undo-failed")


class VolumeGone(PlanError):
    """The library a run worked on cannot be seen, so nothing can be judged."""


def require_library(root: str, categories) -> None:
    """Refuse to judge anything when the library is not where the run left it.

    Without this, a share that is unmounted or that drops half-way looks like a
    library in which every file and folder has vanished.
    """
    if not os.path.isdir(root):
        raise VolumeGone(f"{root} is not there")
    missing = [c for c in categories if not os.path.isdir(os.path.join(root, c))]
    if missing:
        raise VolumeGone(f"{root} is there, but {', '.join(missing)} is not in it: "
                         f"is the right volume mounted?")


def _category(root: str, path: str) -> list:
    rel = os.path.relpath(path, root)
    return [] if rel.startswith(os.pardir) else [rel.split(os.sep, 1)[0]]


def undo_run(conn, run_id: int, quiet: bool = False):
    """Reverse a run, newest op first. Returns (rows undone, rows that failed).

    A row that fails keeps its status, so running undo again tries it again, and
    every mark is committed on its own, so an undo that was cut off is finished
    the same way. Raises VolumeGone, before or part-way, when the library the run
    worked on is not there.
    """
    run = db.get_run(conn, run_id)
    root = run["root"]
    require_library(root, [c for c in (run["categories"] or "").split(",") if c])
    rows = [r for r in db.run_ops(conn, run_id, reverse=True) if r["status"] != "undone"]
    bar = None if quiet else Progress(len(rows), "undo")
    ok = failed = 0
    for row in rows:
        require_library(root, _category(root, row["src"] or row["dst"]))
        try:
            settled = _settle(row)
        except Exception as exc:                      # noqa: BLE001
            db.note_op(conn, row["id"], f"undo: {type(exc).__name__}: {exc}")
            failed += 1
        else:
            if settled:
                db.mark_op(conn, row["id"], "undone", undone=True)
                ok += 1
        conn.commit()
        if bar:
            bar.step(1)
    if bar:
        bar.close()
    return ok, failed


def _settle(row) -> bool:
    """Put the disk back to how it was before this row, if it is not already.

    True means the row is accounted for and can be marked undone. False means an
    unproven row turned out not to have happened, so there is nothing to mark.
    Anything that cannot be decided raises, and the row is left for another try.
    """
    if row["kind"] == "rename":
        return _settle_rename(row)
    if row["kind"] == "mkdir":
        return _settle_mkdir(row)
    if row["kind"] == "rmdir":
        return _settle_rmdir(row)
    return False


def _move_back(src: str, dst: str) -> None:
    if os.path.lexists(src):
        raise PlanError(f"original path reoccupied: {src}")
    os.makedirs(os.path.dirname(src), exist_ok=True)
    os.rename(dst, src)


def _settle_rename(row) -> bool:
    src, dst, inode = row["src"], row["dst"], row["inode"]
    expected = row["status"] in EXPECTED
    if inode:
        if holds(dst, inode):
            _move_back(src, dst)
            return True
        if holds(src, inode):
            return expected         # it is back where it came from, or never left
        if expected:
            raise PlanError(f"cannot find the file this row moved: "
                            f"it is neither at {dst} nor at {src}")
        return False
    # No inode on record, so all that can be known is which paths exist. The
    # planner always records one; only callers that skip it get here.
    if not expected:
        return False
    at_src, at_dst = os.path.lexists(src), os.path.lexists(dst)
    if row["status"] == "started" and at_src:
        if at_dst:
            raise PlanError(f"cannot tell whether this move ran: "
                            f"both {src} and {dst} exist")
        return True                 # the run was cut off before this move happened
    if at_dst:
        _move_back(src, dst)
        return True
    if at_src:
        return True                 # already back
    raise FileNotFoundError(dst)


def _settle_mkdir(row) -> bool:
    """Remove a directory the run created, unless something else lives in it now.

    Folder metadata that appeared inside after the run is no reason to keep the
    directory, and it is not deleted either: it moves up to the parent.
    """
    path = row["dst"]
    expected = row["status"] in EXPECTED
    if not os.path.isdir(path):
        return expected             # already gone, or never made
    names = sorted(os.listdir(path))
    if not all(METADATA_RE.fullmatch(name) for name in names):
        if expected:
            raise PlanError(f"still holds other entries, kept: {path}")
        return False
    parent = os.path.dirname(path)
    for name in names:
        os.rename(os.path.join(path, name), free_name(parent, name))
    os.rmdir(path)
    return True


def _settle_rmdir(row) -> bool:
    """Bring back a directory the run removed."""
    path = row["src"]
    if os.path.isdir(path):
        return row["status"] in EXPECTED    # there again, or never removed
    os.makedirs(path, exist_ok=True)
    return True
