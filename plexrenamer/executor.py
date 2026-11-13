"""Apply a plan. Reversing one is in undo.py, expanding one into operations in ops.py.

The journal is written ahead of the disk: the whole plan is in it before the
first operation, and an operation is marked "started" and committed before it
runs, then "done" or "failed".

The only files the tool deletes in the library are torrent-site ads and
.DS_Store files (junk.py), each with its bytes in the journal first, so undo
writes it back. Besides those it removes only empty directories: a listing must
show one empty, and no "._" file of its name may sit beside it, which is where
some volumes keep a folder's attributes. The speed test removes only the
scratch files it wrote into a new folder of its own, and then that folder,
unless something else is in it.

A path that cannot be looked up is never read as a path that is free or gone.
Only "no such file" means that. Any other error stops the operation in hand.
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
import stat
import tempfile
import time

from . import db, junk
from .ops import TEMP_SUFFIX, Op, PlanError, build_ops, validate  # noqa: F401
from .progress import Progress

# Folder metadata the OS writes by itself. It never counts as content, and a
# move never deletes it: when its folder has to go it moves up to the parent.
# A .DS_Store goes later with the junk (junk.py), its bytes in the journal.
METADATA_RE = re.compile(r"(?:\.DS_Store|Thumbs\.db)(?:-\d+)?")
# The reason on a journal row that was added while the run was under way.
METADATA_MOVE = "folder metadata moved up, its folder is removed"
# How the note on a rename that was refused before os.rename was called begins.
# Such a rename never touched the disk, and undo leaves it be.
NOT_TRIED = "not tried: "


class Unconfirmed(Exception):
    """os.rename itself was called and raised.

    Only then can a rename have gone through although it reported an error,
    which is what a lost reply from a network share looks like. An operation
    that was refused before the call has not touched the disk.
    """

    def __init__(self, cause: OSError):
        super().__init__(f"{type(cause).__name__}: {cause}")


def apply_ops(conn, run_id: int, label: str = "apply", quiet: bool = False):
    """Run every pending op of a run.

    Each row is marked "started" and committed before its operation touches the
    disk, then marked "done" or "failed" and committed again. A run that is cut
    off (Ctrl-C, a kill, a crash) leaves only the rows that were in flight as
    "started", and undo settles those by looking at the disk.
    """
    rows = db.run_ops(conn, run_id)
    todo = [r for r in rows if r["status"] == "pending"]
    bar = None if quiet else Progress(len(todo), label)
    ok = failed = 0
    # Whether a file keeps its inode number when this volume renames it. The first
    # rename shows it. Where it does not, a number proves nothing from then on.
    keeps = None
    for row in todo:
        db.mark_op(conn, row["id"], "started")
        conn.commit()
        try:
            if row["kind"] == "rmdir":
                _lift_metadata(conn, run_id, row["src"])
            elif row["kind"] == "rename":
                _refuse_changed(row)
            elif row["kind"] == "delete":
                junk.keep(conn, row, row["inode"] if keeps is not False else 0)
                db.commit_durably(conn)     # its bytes are in the journal before it goes
            _do(row["kind"], row["src"], row["dst"], row["inode"] if keeps is not False else 0)
        except Exception as exc:                      # noqa: BLE001
            status, note = _outcome(row, keeps, exc)
        else:
            status, note = "done", None
            if keeps is None and row["kind"] == "rename" and row["inode"]:
                keeps = _kept_number(row)
        # Only what the operation itself raised is judged above. A mark that
        # cannot be written stops the run, and the row stays on record as
        # started, which undo settles by looking at the disk.
        db.mark_op(conn, row["id"], status, note)
        ok += status != "failed"
        failed += status == "failed"
        conn.commit()
        if bar:
            bar.step(1, os.path.basename(row["dst"] or row["src"] or ""))
    if bar:
        bar.close()
    return ok, failed


def _outcome(row, keeps, exc) -> tuple:
    """The status and the note for a row whose operation raised.

    A network share can report an error for a rename that went through, when the
    reply is lost. That is asked only when os.rename itself raised. A rename that
    was refused before the call cannot have happened, and asking the disk about
    it would take another file for the one this row was to move. Its note says
    so, for undo. A folder the run was to make that is already there is not the
    run's: made since the plan, or missed then by a lookup that failed.
    """
    if not isinstance(exc, Unconfirmed):
        if row["kind"] == "mkdir" and isinstance(exc, FileExistsError):
            return "undone", f"already there, not made by this run: {row['dst']}"
        note = f"{type(exc).__name__}: {exc}"
        return "failed", NOT_TRIED + note if row["kind"] in ("rename", "delete") else note
    if row["kind"] in ("rename", "delete"):
        try:
            if _took_effect(row, keeps) if row["kind"] == "rename" else not exists(row["src"]):
                return "done", f"took effect although it reported {exc}"
        except OSError as lookup:
            return "failed", (f"{exc}, and whether it took effect could not be read "
                              f"({type(lookup).__name__}: {lookup})")
    return "failed", str(exc)


def _look(path: str):
    """The lstat of path, or None when it is not there.

    Only "no such file" means not there. os.path.lexists and os.path.isdir turn
    every error into False, and on a network share an error can be a hiccup. A
    hiccup must never be read as a file or folder that is gone, so it is raised.
    """
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None


def exists(path: str) -> bool:
    """Whether anything is at path. A lookup error is raised, never read as no."""
    return _look(path) is not None


def is_dir(path: str) -> bool:
    """Whether path is a directory itself, not a link to one. A lookup error is
    raised, never read as no."""
    st = _look(path)
    return st is not None and stat.S_ISDIR(st.st_mode)


def holds(path: str, inode: int) -> bool:
    """True when path exists and is the very file or folder with that inode.

    A lookup error counts as not proven, which is the cautious answer wherever
    this is asked.
    """
    try:
        return db.inode_of(os.lstat(path)) == inode
    except OSError:
        return False


def located(path: str, row, stale: bool) -> bool:
    """True when what this journal row moved is at path.

    Name, kind and size identify it. The inode is further proof, but only while
    the journal's numbers still tell files apart, and `stale` says that they no
    longer do.
    """
    st = _look(path)
    if st is None:
        return False
    if bool(row["is_dir"]) != stat.S_ISDIR(st.st_mode):
        return False
    if row["size"] and st.st_size != row["size"]:
        return False
    return stale or not row["inode"] or db.inode_of(st) == row["inode"]


def inodes_are_stale(rows, rewritten=()) -> bool:
    """Whether the journal's inode numbers no longer tell this run's files apart.

    A number proves which file is which only where a file keeps its number when
    it moves, and only while the mount lasts. A network share can number its
    files anew on every mount, or work each number out from the path, which
    gives a file another one with every rename. Either way a file the run moved
    no longer carries the number on record, so the moved files are the ones to
    ask: the rows whose file is at its destination, by kind and size, decide.
    Only when no row is, the rows whose file is at its source do. A file at its
    source proves less, because a number worked out from the path is always
    the recorded one there.

    A path that cannot be looked up raises. Skipping it could hand the decision
    to the weaker evidence, and that answer steers every row of the run. A file
    that a later delete took and undo wrote back has a number of its own anyway,
    so its row does not count: a delete of the run, or one of a later run whose
    paths are in `rewritten`.
    """
    rows = list(rows)
    deleted = {r["src"]: r["seq"] for r in rows if r["kind"] == "delete"}
    for end in ("dst", "src"):
        found = matched = 0
        for row in rows:
            if row["kind"] != "rename" or not row["inode"] or row["status"] == "undone" \
                    or row["dst"] in rewritten \
                    or (row["dst"] in deleted and deleted[row["dst"]] > row["seq"]):
                continue
            st = _look(row[end])
            if st is None:
                continue
            if bool(row["is_dir"]) != stat.S_ISDIR(st.st_mode):
                continue
            if row["size"] and st.st_size != row["size"]:
                continue
            found += 1
            matched += db.inode_of(st) == row["inode"]
            if found >= 200:
                break
        if found:
            return matched * 2 < found
    return False


def _took_effect(row, keeps) -> bool:
    """A rename is proven done when its file is at the destination and gone from
    the source.

    The inode proves that it is that file. On a volume that has shown that a
    moved file gets another number (`keeps` is False), its kind and size have
    to. Without a recorded inode, or without a size where the number cannot be
    asked, nothing is proven. A lookup error is raised: the caller records the
    rename as failed, and undo settles it by the disk.
    """
    if not row["inode"] or exists(row["src"]):
        return False
    if keeps is False:
        return bool(row["size"]) and located(row["dst"], row, stale=True)
    return holds(row["dst"], row["inode"])


def _kept_number(row):
    """Whether the file a rename has just moved still has the inode number on
    record. None when its destination cannot be read right now, so that one
    hiccup does not settle the question for the whole run."""
    try:
        return db.inode_of(os.lstat(row["dst"])) == row["inode"]
    except OSError:
        return None


def _refuse_changed(row) -> None:
    """Stop a rename whose source is no longer, by kind and size, what was planned.

    A file that has grown or been replaced since the plan was made stays where it
    is. Moving it would put a file into the journal that undo could not find
    again, because undo knows a file by its name, kind and size. A source that
    is not there at all is left for the rename itself to report. A file planned
    at 0 bytes has no size to compare, so it is checked by kind alone.
    """
    st = _look(row["src"])
    if st is None:
        return
    if bool(row["is_dir"]) != stat.S_ISDIR(st.st_mode):
        raise PlanError(f"no longer a {'folder' if row['is_dir'] else 'file'}: {row['src']}")
    if row["size"] and st.st_size != row["size"]:
        raise PlanError(f"size changed for {row['src']}: planned {row['size']} bytes, "
                        f"now {st.st_size}")


def free_name(parent: str, name: str) -> str:
    """A metadata name not yet taken in parent: .DS_Store, .DS_Store-1, ...

    A name that cannot be looked up raises. Read as free, it would let the
    rename that follows replace the parent's own metadata.
    """
    base = re.sub(r"-\d+$", "", name)
    target, n = os.path.join(parent, base), 0
    while exists(target):
        n += 1
        target = os.path.join(parent, f"{base}-{n}")
    return target


def keep_attributes(path: str) -> None:
    """Refuse to remove a folder that has a "._" file of its name beside it.

    A volume without extended attributes of its own (FAT, exFAT, some network
    shares) keeps a folder's attributes in such a file, and macOS deletes that
    file together with the folder. A listing of the folder does not show it.
    """
    beside = os.path.join(os.path.dirname(path), "._" + os.path.basename(path))
    if exists(beside):
        raise PlanError(f"{os.path.basename(beside)} beside it would go with it, "
                        f"left in place: {path}")


def remove_empty(path: str) -> None:
    """Remove a directory, and only one that a listing shows to be empty.

    rmdir alone is no guard. A system can remove a directory together with files
    it regards as disposable: macOS does so with one that holds only "._" files,
    and with the "._" file beside it that holds the folder's attributes. So the
    directory is listed first, and anything in it keeps it, as does such a file.
    """
    if not is_dir(path):
        return
    if os.listdir(path):
        raise PlanError(f"not empty, left in place: {path}")
    keep_attributes(path)
    try:
        os.rmdir(path)
    except OSError as exc:
        if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
            raise PlanError(f"not empty, left in place: {path}") from exc
        raise


def _lift_metadata(conn, run_id: int, directory: str) -> None:
    """Get folder metadata out of the way of an rmdir without deleting it.

    A `.DS_Store` or `Thumbs.db` is all that stops an emptied directory from
    being removed. It moves up to the parent, and that move goes into the journal
    as a rename of its own, written before it happens, so undo brings it back.
    Nothing moves when the directory holds anything else: it stays as it is and
    the rmdir reports that it was not empty. Nothing moves either when the
    directory is to stay for the "._" file beside it.
    """
    if not is_dir(directory):
        return
    names = sorted(os.listdir(directory))
    if not names or not all(METADATA_RE.fullmatch(name) for name in names):
        return
    keep_attributes(directory)
    parent = os.path.dirname(directory)
    for name in names:
        src = os.path.join(directory, name)
        dst = free_name(parent, name)
        info = os.lstat(src)
        inode = db.inode_of(info)
        op_id = db.append_op(conn, run_id, "rename", src, dst, size=info.st_size,
                             inode=inode, status="started", reason=METADATA_MOVE)
        conn.commit()
        _do("rename", src, dst, inode)
        db.mark_op(conn, op_id, "done")
        conn.commit()


def _do(kind: str, src, dst, inode):
    if kind == "mkdir":
        os.mkdir(dst)                   # a folder already there raises: it is not the run's
        return
    if kind == "rmdir":
        remove_empty(src)
        return
    if kind == "delete":
        try:
            os.unlink(src)
        except OSError as exc:
            raise Unconfirmed(exc) from exc
        return
    if not exists(src):
        raise FileNotFoundError(src)
    if inode:
        actual = db.inode_of(os.lstat(src))
        if actual != inode:
            raise PlanError(f"inode changed for {src}: expected {inode}, saw {actual}")
    if exists(dst):
        raise PlanError(f"destination occupied: {dst}")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.rename(src, dst)
    except OSError as exc:
        raise Unconfirmed(exc) from exc


def verify_run(conn, run_id: int):
    """Confirm on-disk reality matches the journal.

    Raises OSError when the library cannot be read well enough to judge the run
    as a whole. A single row that cannot be read counts as a mismatch.
    """
    good = bad = 0
    problems = []
    rows = db.run_ops(conn, run_id)
    kept = db.kept_of_run(conn, run_id)
    stale = inodes_are_stale(rows)
    # A file can move twice in one run: a swap staged through a temporary name,
    # or folder metadata lifted out of two nested folders. Its first destination
    # is then the source of a later move, and that later row is the one to check.
    # So is a later delete, of an ad a move hid or of a .DS_Store a move lifted.
    moved_on = {}
    for row in rows:
        if row["status"] == "done" and row["kind"] in ("rename", "delete"):
            moved_on[row["src"]] = max(moved_on.get(row["src"], 0), row["seq"])
    for row in rows:
        if row["status"] != "done":
            continue
        try:
            problem = _mismatch(row, moved_on, stale, kept)
        except OSError as exc:
            problem = f"cannot be read ({type(exc).__name__}: {exc}): {row['dst'] or row['src']}"
        if problem:
            problems.append(problem)
            bad += 1
        elif row["kind"] in ("rename", "delete"):
            good += 1
    return good, bad, problems


def _mismatch(row, moved_on, stale, kept=None):
    """What is wrong on disk with a row the journal has as done, or None."""
    if row["kind"] == "delete":
        return junk.still_there(row, (kept or {}).get(row["id"]))
    if row["kind"] == "rename":
        if moved_on.get(row["dst"], 0) > row["seq"]:
            return None
        if not exists(row["dst"]):
            return f"missing destination: {row['dst']}"
        if not located(row["dst"], row, stale):
            return f"not what the run moved there: {row['dst']}"
    elif row["kind"] == "mkdir" and not is_dir(row["dst"]):
        return f"missing directory: {row['dst']}"
    return None


def benchmark(volume_root: str, samples: int = 60) -> float:
    """Measure rename throughput on the target filesystem, in ops/second.

    It works in a folder of its own, made new under a name no other folder has,
    and removes only what it made there: its scratch files, then the folder.
    Anything else that turns up in the folder keeps it.
    """
    scratch = tempfile.mkdtemp(prefix=".plexrenamer-benchmark-", dir=volume_root)
    made = []
    try:
        for i in range(samples):
            p = os.path.join(scratch, f"bench-{i:03d}")
            with open(p, "x") as fh:
                fh.write("x")
            made.append(p)
        start = time.time()
        for p in made:
            os.rename(p, p + ".moved")
        for p in made:
            os.rename(p + ".moved", p)
        elapsed = time.time() - start
        return (samples * 2) / elapsed if elapsed > 0 else 500.0
    finally:
        for p in made:
            for name in (p, p + ".moved"):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(name)
        with contextlib.suppress(OSError, PlanError):
            remove_empty(scratch)
