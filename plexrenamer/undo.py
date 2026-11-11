"""Reverse a run.

Undo is built on two things that survive whatever stopped a run: the plan, which
is in the journal before the first operation, and the disk. The status of a row
only says what to expect. Each row is settled by where the thing it moved is
now, found by name, kind and size, so undo never moves what it cannot identify.
The inode is extra proof while the journal's numbers still hold; a network share
numbers its files anew on every mount, and then they are not asked for.

A row that cannot be settled keeps its status and is tried again by the next
undo. Nothing is marked as undone that is not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import db
from .executor import (METADATA_MOVE, METADATA_RE, PlanError, Progress, exists,
                       free_name, inodes_are_stale, located)

# What the journal says about a row before undo looks at the disk. An expected
# row ran, or was in flight when its run was cut off ("started"), so it has to be
# accounted for. An unproven row reported an error ("failed") or was never marked
# ("pending"): it is reversed only when the disk shows that it happened after all,
# which is what a lost reply from a network share or a lost journal mark looks like.
EXPECTED = ("done", "started", "undo-failed")


class VolumeGone(PlanError):
    """The library a run worked on cannot be seen, so nothing can be judged."""


@dataclass
class _Run:
    """What undo works out about a run before it settles a single row."""
    stale: bool     # the journal's inode numbers are from another mount
    reached: int    # the highest planned row the run is known to have got to
    owned: set      # destinations of this run's renames that are not moved back


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


def _survey(rows) -> _Run:
    stale = inodes_are_stale(rows)
    planned = [r for r in rows if r["reason"] != METADATA_MOVE]
    # Operations run in journal order, so a row that is marked at all shows the
    # run got at least that far. So does an unmarked rename whose file is at its
    # destination.
    reached = max((r["seq"] for r in planned if r["status"] != "pending"), default=0)
    for r in planned:
        if r["kind"] == "rename" and r["status"] == "pending" and r["seq"] > reached \
                and located(r["dst"], r, stale) and not exists(r["src"]):
            reached = r["seq"]
    owned = {r["dst"] for r in rows if r["kind"] == "rename" and r["status"] != "undone"}
    return _Run(stale, reached, owned)


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
    every = db.run_ops(conn, run_id, reverse=True)
    state = _survey(every)
    rows = [r for r in every if r["status"] != "undone"]
    bar = None if quiet else Progress(len(rows), "undo")
    ok = failed = 0
    for row in rows:
        require_library(root, _category(root, row["src"] or row["dst"]))
        try:
            settled = _settle(row, state)
        except Exception as exc:                      # noqa: BLE001
            db.note_op(conn, row["id"], f"undo: {type(exc).__name__}: {exc}")
            failed += 1
        else:
            if settled:
                db.mark_op(conn, row["id"], "undone", undone=True)
                state.owned.discard(row["dst"])
                ok += 1
        conn.commit()
        if bar:
            bar.step(1)
    if bar:
        bar.close()
    return ok, failed


def _settle(row, state: _Run) -> bool:
    """Put the disk back to how it was before this row, if it is not already.

    True means the row is accounted for and can be marked undone. False means an
    unproven row turned out not to have happened, so there is nothing to mark.
    Anything that cannot be decided raises, and the row is left for another try.
    """
    if row["kind"] == "rename":
        return _settle_rename(row, state)
    if row["status"] not in EXPECTED and row["seq"] > state.reached:
        return False                # the run never got as far as this folder
    if row["kind"] == "mkdir":
        return _settle_mkdir(row, state)
    if row["kind"] == "rmdir":
        return _settle_rmdir(row)
    return False


def _settle_rename(row, state: _Run) -> bool:
    src, dst = row["src"], row["dst"]
    # A size, or an inode that still holds, tells this row's file from any other.
    # Without either, a name is all there is, and a name alone is no ground to
    # move a file for a row the journal does not say was carried out.
    identified = bool(row["size"]) or (bool(row["inode"]) and not state.stale)
    if row["status"] not in EXPECTED and not identified:
        return False
    if located(dst, row, state.stale):
        if exists(src):
            raise PlanError(f"original path reoccupied: {src}")
        os.makedirs(os.path.dirname(src), exist_ok=True)
        os.rename(dst, src)
        return True
    if located(src, row, state.stale):
        return row["status"] in EXPECTED    # it is back, or it never left
    if row["status"] not in EXPECTED:
        return False                        # nothing shows that this move was made
    raise PlanError(f"cannot find what this row moved: it is neither at {dst} "
                    f"nor at {src}")


def _settle_mkdir(row, state: _Run) -> bool:
    """Remove a directory the run created, unless something else lives in it now.

    Folder metadata that appeared inside after the run is no reason to keep the
    directory, and it is not deleted either: it moves up to the parent.
    """
    path = row["dst"]
    if not exists(path):
        return row["status"] in EXPECTED    # already gone, or never made
    names = sorted(os.listdir(path))
    mine = [n for n in names if os.path.join(path, n) in state.owned]
    if mine:
        # a file this run put here under a metadata name is not metadata
        raise PlanError(f"still holds {mine[0]}, which this run has not moved "
                        f"back: {path}")
    if not all(METADATA_RE.fullmatch(name) for name in names):
        raise PlanError(f"still holds other entries, kept: {path}")
    parent = os.path.dirname(path)
    for name in names:
        os.rename(os.path.join(path, name), free_name(parent, name))
    os.rmdir(path)
    return True


def _settle_rmdir(row) -> bool:
    """Bring back a directory the run removed."""
    path = row["src"]
    if exists(path):
        return row["status"] in EXPECTED    # there again, or never removed
    os.makedirs(path, exist_ok=True)
    return True
