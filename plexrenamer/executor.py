"""Apply a plan. Reversing one is in undo.py.

Ordering rules:
  1. every destination directory is created first (recorded, so undo can remove it)
  2. file moves run in an order that never overwrites a file that still has to move
  3. genuine cycles are broken with a temporary name, recorded as two moves
  4. directories we emptied are removed last, deepest first, and recreated on undo
  5. the journal is written ahead of the disk: the whole plan is in it before the
     first op, and an op is marked "started" and committed before it runs, then
     "done" or "failed"

Nothing in the library is ever deleted. The only things removed are directories
this tool emptied or created.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass

from . import db
from .progress import Progress

TEMP_SUFFIX = ".plexrenamer-tmp"
# Folder metadata the OS writes by itself. It never counts as content, and it is
# never deleted: when its folder has to go it moves up to the parent.
METADATA_RE = re.compile(r"(?:\.DS_Store|Thumbs\.db)(?:-\d+)?")
# The reason on a journal row that was added while the run was under way.
METADATA_MOVE = "folder metadata moved up, its folder is removed"


@dataclass
class Op:
    seq: int
    kind: str          # mkdir | rename | rmdir
    src: str | None
    dst: str | None
    is_dir: bool
    size: int
    inode: int
    category: str
    reason: str


class PlanError(Exception):
    pass


def validate(moves):
    """Reject anything that could lose data before a single rename happens."""
    problems = []
    by_dst = {}
    srcs = set()
    for m in moves:
        if m.src in srcs:
            problems.append(f"source listed twice: {m.src}")
        srcs.add(m.src)
        by_dst.setdefault(m.dst, []).append(m.src)
    for dst, sources in by_dst.items():
        if len(sources) > 1:
            problems.append("collision: " + " + ".join(sources) + f"  ->  {dst}")
    for m in moves:
        if not os.path.lexists(m.src):
            problems.append(f"source vanished: {m.src}")
        if os.path.lexists(m.dst) and m.dst not in srcs and os.path.normpath(
                m.dst) != os.path.normpath(m.src):
            problems.append(f"destination already occupied: {m.dst}")
    return problems


def build_ops(moves, boundaries=(), extra_prunes=()):
    """Expand moves into the flat, ordered, reversible op list.

    `boundaries` are the only directories whose interior may be pruned. Nothing
    at or above a boundary is ever removed, so a run can never climb out of the
    library and remove the volume root.
    """
    srcs = {m.src for m in moves}
    existing_dirs = set()
    needed_dirs = []
    for m in moves:
        parent = os.path.dirname(m.dst)
        chain = []
        while parent and not os.path.isdir(parent) and parent not in existing_dirs:
            chain.append(parent)
            existing_dirs.add(parent)
            parent = os.path.dirname(parent)
        needed_dirs.extend(reversed(chain))

    ops, seq = [], 0
    for path in needed_dirs:
        seq += 1
        ops.append(Op(seq, "mkdir", None, path, True, 0, 0, "", "create destination"))

    pending = list(moves)
    done_srcs = set()
    tail = []
    temp_n = 0
    while pending:
        progressed = False
        deferred = []
        for m in pending:
            blocked = (os.path.lexists(m.dst) and m.dst not in done_srcs
                       and m.dst in srcs)
            if blocked:
                deferred.append(m)
                continue
            seq += 1
            ops.append(Op(seq, "rename", m.src, m.dst, m.is_dir, m.size, m.inode,
                          m.category, m.reason))
            done_srcs.add(m.src)
            progressed = True
        if not deferred:
            break
        if not progressed:
            # a genuine cycle: stage one member through a temporary name
            victim = deferred.pop(0)
            temp_n += 1
            tmp = f"{victim.src}{TEMP_SUFFIX}{temp_n}"
            seq += 1
            ops.append(Op(seq, "rename", victim.src, tmp, victim.is_dir, victim.size,
                          victim.inode, victim.category, "cycle break"))
            # the second leg has to wait until whatever occupies the destination
            # has moved out, so it is appended after every ordinary move
            tail.append((tmp, victim))
            done_srcs.add(victim.src)
            srcs.discard(victim.src)
        pending = deferred

    for tmp, victim in tail:
        seq += 1
        ops.append(Op(seq, "rename", tmp, victim.dst, victim.is_dir, victim.size,
                      victim.inode, victim.category, victim.reason))

    prunable = _prunable_dirs(moves, {o.dst for o in ops if o.kind == "mkdir"},
                              boundaries)
    for extra in extra_prunes:
        if extra not in prunable and _inside(extra, boundaries) \
                and os.path.isdir(extra):
            prunable.append(extra)
    prunable.sort(key=lambda p: p.count(os.sep), reverse=True)
    for path in prunable:
        seq += 1
        ops.append(Op(seq, "rmdir", path, None, True, 0, 0, "", "emptied by this run"))
    return ops


def _inside(path: str, boundaries) -> bool:
    path = os.path.normpath(path)
    for b in boundaries:
        b = os.path.normpath(b)
        if path != b and path.startswith(b + os.sep):
            return True
    return False


def _prunable_dirs(moves, created, boundaries=()):
    """Source directories this run will empty, deepest first.

    Only directories strictly inside a boundary are ever considered, so the
    walk cannot reach the library root, /Volumes, or /.
    """
    if not boundaries:
        return []
    touched = set()
    for m in moves:
        d = os.path.dirname(m.src)
        while d and d not in touched and _inside(d, boundaries):
            touched.add(d)
            d = os.path.dirname(d)
    leaving = {}
    for m in moves:
        leaving.setdefault(os.path.dirname(m.src), 0)
        leaving[os.path.dirname(m.src)] += 1
    # never prune a directory that still holds something after the run
    keep = set()
    for m in moves:
        d = os.path.dirname(m.dst)
        while d and d not in keep:
            keep.add(d)
            d = os.path.dirname(d)

    candidates = []
    for d in touched:
        if d in created or d in keep or not os.path.isdir(d) \
                or not _inside(d, boundaries):
            continue
        try:
            entries = [e for e in os.listdir(d) if e not in {".DS_Store"}]
        except OSError:
            continue
        remaining = [e for e in entries
                     if os.path.join(d, e) not in {m.src for m in moves}]
        if remaining and not all(os.path.isdir(os.path.join(d, e)) for e in remaining):
            continue
        candidates.append(d)
    return sorted(candidates, key=lambda p: p.count(os.sep), reverse=True)


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
            _do(row["kind"], row["src"], row["dst"], row["inode"] if keeps is not False else 0)
            if keeps is None and row["kind"] == "rename" and row["inode"]:
                keeps = _kept_number(row)
            db.mark_op(conn, row["id"], "done")
            ok += 1
        except Exception as exc:                      # noqa: BLE001
            if row["kind"] == "rename" and _took_effect(row, keeps):
                # a network share can report an error for a rename that went
                # through, when the reply is lost
                db.mark_op(conn, row["id"], "done",
                           f"took effect although it reported {type(exc).__name__}: {exc}")
                ok += 1
            else:
                db.mark_op(conn, row["id"], "failed", f"{type(exc).__name__}: {exc}")
                failed += 1
        conn.commit()
        if bar:
            bar.step(1, os.path.basename(row["dst"] or row["src"] or ""))
    if bar:
        bar.close()
    return ok, failed


def holds(path: str, inode: int) -> bool:
    """True when path exists and is the very file or folder with that inode."""
    try:
        return db.inode_of(os.lstat(path)) == inode
    except OSError:
        return False


def exists(path: str) -> bool:
    """Like os.path.lexists, except that only "no such file" means it is not there.

    lexists turns every error into False. On a network share an error can be a
    hiccup, and a hiccup must never be read as a file or folder that is gone.
    """
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def located(path: str, row, stale: bool) -> bool:
    """True when what this journal row moved is at path.

    Name, kind and size identify it. The inode is further proof, but only while
    the journal's numbers still tell files apart, and `stale` says that they no
    longer do.
    """
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    if bool(row["is_dir"]) != stat.S_ISDIR(st.st_mode):
        return False
    if row["size"] and st.st_size != row["size"]:
        return False
    return stale or not row["inode"] or db.inode_of(st) == row["inode"]


def inodes_are_stale(rows) -> bool:
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
    """
    for end in ("dst", "src"):
        found = matched = 0
        for row in rows:
            if row["kind"] != "rename" or not row["inode"] or row["status"] == "undone":
                continue
            try:
                st = os.lstat(row[end])
            except OSError:
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
    asked, nothing is proven.
    """
    if not row["inode"] or os.path.lexists(row["src"]):
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
    is not there at all is left for the rename itself to report.
    """
    try:
        st = os.lstat(row["src"])
    except OSError:
        return
    if bool(row["is_dir"]) != stat.S_ISDIR(st.st_mode):
        raise PlanError(f"no longer a {'folder' if row['is_dir'] else 'file'}: {row['src']}")
    if row["size"] and st.st_size != row["size"]:
        raise PlanError(f"size changed for {row['src']}: planned {row['size']} bytes, "
                        f"now {st.st_size}")


def free_name(parent: str, name: str) -> str:
    """A metadata name not yet taken in parent: .DS_Store, .DS_Store-1, ..."""
    base = re.sub(r"-\d+$", "", name)
    target, n = os.path.join(parent, base), 0
    while os.path.lexists(target):
        n += 1
        target = os.path.join(parent, f"{base}-{n}")
    return target


def _lift_metadata(conn, run_id: int, directory: str) -> None:
    """Get folder metadata out of the way of an rmdir without deleting it.

    A `.DS_Store` or `Thumbs.db` is all that stops an emptied directory from
    being removed. It moves up to the parent, and that move goes into the journal
    as a rename of its own, written before it happens, so undo brings it back.
    Nothing moves when the directory holds anything else: it stays as it is and
    the rmdir reports that it was not empty.
    """
    if not os.path.isdir(directory):
        return
    names = sorted(os.listdir(directory))
    if not names or not all(METADATA_RE.fullmatch(name) for name in names):
        return
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
        os.makedirs(dst, exist_ok=True)
        return
    if kind == "rmdir":
        if not os.path.isdir(src):
            return
        try:
            os.rmdir(src)
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                raise PlanError(f"not empty, left in place: {src}") from exc
            raise
        return
    if not os.path.lexists(src):
        raise FileNotFoundError(src)
    if inode:
        actual = db.inode_of(os.lstat(src))
        if actual != inode:
            raise PlanError(f"inode changed for {src}: expected {inode}, saw {actual}")
    if os.path.lexists(dst):
        raise PlanError(f"destination occupied: {dst}")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.rename(src, dst)


def verify_run(conn, run_id: int):
    """Confirm on-disk reality matches the journal."""
    good = bad = 0
    problems = []
    rows = db.run_ops(conn, run_id)
    stale = inodes_are_stale(rows)
    # A file can move twice in one run: a swap staged through a temporary name,
    # or folder metadata lifted out of two nested folders. Its first destination
    # is then the source of a later move, and that later row is the one to check.
    moved_on = {}
    for row in rows:
        if row["status"] == "done" and row["kind"] == "rename":
            moved_on[row["src"]] = max(moved_on.get(row["src"], 0), row["seq"])
    for row in rows:
        if row["status"] != "done":
            continue
        if row["kind"] == "rename":
            if moved_on.get(row["dst"], 0) > row["seq"]:
                good += 1
            elif not exists(row["dst"]):
                problems.append(f"missing destination: {row['dst']}")
                bad += 1
            elif not located(row["dst"], row, stale):
                problems.append(f"not what the run moved there: {row['dst']}")
                bad += 1
            else:
                good += 1
        elif row["kind"] == "mkdir" and not os.path.isdir(row["dst"]):
            problems.append(f"missing directory: {row['dst']}")
            bad += 1
    return good, bad, problems


def benchmark(volume_root: str, samples: int = 60) -> float:
    """Measure rename throughput on the target filesystem, in ops/second."""
    scratch = os.path.join(volume_root, ".plexrenamer-benchmark")
    os.makedirs(scratch, exist_ok=True)
    try:
        paths = []
        for i in range(samples):
            p = os.path.join(scratch, f"bench-{i:03d}")
            with open(p, "w") as fh:
                fh.write("x")
            paths.append(p)
        start = time.time()
        for p in paths:
            os.rename(p, p + ".moved")
        for p in paths:
            os.rename(p + ".moved", p)
        elapsed = time.time() - start
        rate = (samples * 2) / elapsed if elapsed > 0 else 500.0
        for p in paths:
            os.remove(p)
        return rate
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
