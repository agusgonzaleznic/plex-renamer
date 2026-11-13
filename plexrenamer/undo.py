"""Reverse a run.

Undo is built on two things that survive whatever stopped a run: the plan, which
is in the journal before the first operation, and the disk. The status of a row
only says what to expect. Each row is settled by where the thing it moved is
now, found by name, kind and size, so undo never moves what it cannot identify.
The inode is extra proof while the journal's numbers still tell files apart. A
network share can number its files anew on every mount, or work each number out
from the path, and then the numbers are not asked for.

Without the numbers, two things of one kind and size cannot be told apart, and a
folder or an empty file has no size either. Where a run was to move such
look-alikes along one path (a swap, or a to b and b to c), the journal decides
what the disk cannot: a row behind the last one that carries a mark never ran.
What that does not decide is reported and stays as it is. It rests on the marks
the run wrote: a journal that lost some can make such a swap or cycle look as
if it never got that far.

A row that cannot be settled keeps its status and is tried again by the next
undo. Nothing is marked as undone that is not, as long as the journal holds the
marks the run wrote.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import db
from .executor import (METADATA_MOVE, METADATA_RE, NOT_TRIED, PlanError, exists, free_name,
                       inodes_are_stale, is_dir, keep_attributes, located, remove_empty)
from .progress import Progress

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
    stale: bool     # the journal's inode numbers no longer tell files apart
    marked: int     # the highest planned row that carries any mark
    reached: int    # the highest planned row the run is known to have got to
    owned: set      # destinations of this run's renames that are not moved back
    leaving: dict   # a path -> the renames of this run that move something away from it
    # the paths of renames that could not be undone in this pass
    blocked: set = field(default_factory=set)
    # the paths a later run that still stands has not undone, and every folder above them
    held: set = field(default_factory=set)
    around: set = field(default_factory=set)


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
    # Operations run in journal order, and each is marked before it runs. So a
    # row that is marked at all shows the run got at least that far, and the rows
    # behind the last marked one never ran, unless marks were lost.
    marked = reached = max((r["seq"] for r in planned if r["status"] != "pending"),
                           default=0)
    # A lost mark shows on disk: an unmarked rename whose file is at its
    # destination was carried out.
    for r in planned:
        if r["kind"] == "rename" and r["status"] == "pending" and r["seq"] > reached \
                and located(r["dst"], r, stale) and not exists(r["src"]):
            reached = r["seq"]
    owned = {r["dst"] for r in rows if r["kind"] == "rename" and r["status"] != "undone"}
    leaving = {}
    for r in rows:
        if r["kind"] == "rename":
            leaving.setdefault(r["src"], []).append(r)
    return _Run(stale, marked, reached, owned, leaving)


def _folders_above(path: str):
    parent = os.path.dirname(path)
    while parent and parent != os.path.dirname(parent):
        yield parent
        parent = os.path.dirname(parent)


def _hold_for_later_runs(conn, run_id: int, root: str, state: _Run) -> None:
    """Keep undo off every path a later run on this library still stands on.

    A run is undone before a later one that is not fully undone only with
    `--force`. The later run may have moved look-alikes along the same paths,
    and what it left there can pass for what this run put there. So a row of
    this run waits when it names such a path, a path inside it, or a folder
    that holds it.
    """
    for later in db.standing_runs_after(conn, run_id, root):
        for r in db.run_ops(conn, later):
            if r["status"] != "undone":
                state.held.update(p for p in (r["src"], r["dst"]) if p)
    for path in state.held:
        state.around.update(_folders_above(path))


def _later_run_on(row, state: _Run):
    """The first path of row that a later run still stands on, or None."""
    for path in (row["src"], row["dst"]):
        if path and (path in state.held or path in state.around
                     or any(folder in state.held for folder in _folders_above(path))):
            return path
    return None


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
    try:
        state = _survey(every)
    except OSError as exc:
        # judged on half a look, the run could be settled by the wrong evidence
        raise VolumeGone(f"the library cannot be read right now "
                         f"({type(exc).__name__}: {exc})") from exc
    _hold_for_later_runs(conn, run_id, root, state)
    rows = [r for r in every if r["status"] != "undone"]
    bar = None if quiet else Progress(len(rows), "undo")
    ok = failed = 0
    for row in rows:
        require_library(root, _category(root, row["src"] or row["dst"]))
        try:
            settled = _settle(row, state)
        except Exception as exc:                      # noqa: BLE001
            db.note_op(conn, row["id"], f"undo: {type(exc).__name__}: {exc}")
            if row["kind"] == "rename":
                # An older row on one of these paths waits. Settled now, it would
                # take what this row left there for its own file.
                state.blocked.update((row["src"], row["dst"]))
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
    if row["kind"] == "rename" and row["status"] == "failed" \
            and (row["error"] or "").startswith(NOT_TRIED):
        return False                # refused before the rename: the disk never saw it
    held = _later_run_on(row, state)
    if held:
        raise PlanError(f"waits for a later run that still stands on {held}: undo it first")
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
    if src in state.blocked or dst in state.blocked:
        raise PlanError("waits for a newer row of this run on the same path, which "
                        "could not be undone")
    expected = row["status"] in EXPECTED
    # Blind: no inode tells this row's file from another, so name, kind and size
    # have to. Not identified: there is no size either, as for a folder or an
    # empty file, and a name alone is no ground to move anything for a row the
    # journal does not say was carried out.
    blind = state.stale or not row["inode"]
    identified = bool(row["size"]) or not blind
    at_dst = located(dst, row, state.stale)
    rival = _rival(row, state) if at_dst and blind else None
    if rival is not None and rival["status"] == "undone":
        at_dst, rival = False, None     # what is there is the file that row took back
    if not at_dst:
        if located(src, row, state.stale):
            return expected             # it is back, or it never left
        if expected:
            raise PlanError(f"cannot find what this row moved: it is neither at {dst} "
                            f"nor at {src}")
        return False                    # nothing shows that this move was made
    gone = not exists(src)
    doubt = None if rival is None else (
        f"cannot tell what is at {dst}: what this row moved there, or what row "
        f"{rival['seq']} was to move away from there. They are of one kind and size")
    if expected:
        if doubt and row["status"] == "started":
            raise PlanError(doubt)
        if not gone:
            raise PlanError(f"original path reoccupied: {src}")
    elif doubt and row["seq"] > state.marked and row["reason"] != METADATA_MOVE:
        return False                    # behind the last marked row: it never ran
    elif not gone:
        if not identified:
            if row["seq"] > state.reached:
                return False            # behind the last mark: it never ran
            raise PlanError(f"cannot tell whether this run made this move: something is "
                            f"at {dst} and at {src}, and neither a size nor an inode says "
                            f"which is what the row was to move")
        raise PlanError(doubt or f"original path reoccupied: {src}")
    elif doubt:
        raise PlanError(doubt)
    elif not identified:
        raise PlanError(f"cannot tell whether this run made this move: something is at "
                        f"{dst} and nothing at {src}, and neither a size nor an inode "
                        f"says whether it is what the row was to move")
    os.makedirs(os.path.dirname(src), exist_ok=True)
    os.rename(dst, src)
    return True


def _rival(row, state: _Run):
    """An older row of the run that moves a look-alike away from this row's destination.

    Unless that row is known to have run, what is at the destination can be its
    file, which never left, or which undo has taken back there. Returns the row,
    or None.
    """
    for other in state.leaving.get(row["dst"], ()):
        if other["seq"] < row["seq"] and bool(other["is_dir"]) == bool(row["is_dir"]) \
                and (not row["size"] or other["size"] == row["size"]) \
                and other["status"] not in ("done", "undo-failed"):
            return other
    return None


def _settle_mkdir(row, state: _Run) -> bool:
    """Remove a directory the run created, unless something else lives in it now.

    Folder metadata that appeared inside after the run is no reason to keep the
    directory, and it is not deleted either: it moves up to the parent.
    """
    path = row["dst"]
    if not is_dir(path):
        # already gone, or never made: a file in its place is not this run's
        return row["status"] in EXPECTED
    names = sorted(os.listdir(path))
    mine = [n for n in names if os.path.join(path, n) in state.owned]
    if mine:
        # a file this run put here under a metadata name is not metadata
        raise PlanError(f"still holds {mine[0]}, which this run has not moved "
                        f"back: {path}")
    if not all(METADATA_RE.fullmatch(name) for name in names):
        raise PlanError(f"still holds other entries, kept: {path}")
    keep_attributes(path)               # before anything moves up
    parent = os.path.dirname(path)
    for name in names:
        os.rename(os.path.join(path, name), free_name(parent, name))
    remove_empty(path)
    return True


def _settle_rmdir(row) -> bool:
    """Bring back a directory the run removed, unless something else has its name."""
    path = row["src"]
    if is_dir(path):
        return row["status"] in EXPECTED    # there again, or never removed
    if exists(path):
        raise PlanError(f"something else has its name now, so it is not made again: {path}")
    os.makedirs(path, exist_ok=True)
    return True
