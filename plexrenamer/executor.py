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
import sys
import time
from dataclasses import dataclass

from . import db

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


class Progress:
    def __init__(self, total: int, label: str = "", width: int = 38):
        self.total, self.label, self.width = max(total, 1), label, width
        self.n = 0
        self.start = time.time()
        self.tty = sys.stdout.isatty()
        self.last = 0.0
        self.last_pct = -1

    def step(self, n: int = 1, note: str = ""):
        self.n += n
        now = time.time()
        pct = int(self.n * 100 / self.total)
        if self.tty:
            if now - self.last < 0.08 and self.n < self.total:
                return
        elif pct == self.last_pct and self.n < self.total:
            return
        self.last, self.last_pct = now, pct
        self.render(note)

    def render(self, note: str = ""):
        elapsed = time.time() - self.start
        rate = self.n / elapsed if elapsed > 0 else 0
        eta = (self.total - self.n) / rate if rate > 0 else 0
        filled = int(self.width * self.n / self.total)
        bar = "#" * filled + "." * (self.width - filled)
        line = (f"{self.label} [{bar}] {self.n}/{self.total} "
                f"{self.n * 100 / self.total:5.1f}%  {rate:6.1f}/s  "
                f"eta {fmt_duration(eta)}")
        if note:
            line += f"  {note[:48]}"
        if self.tty:
            sys.stdout.write("\r" + line.ljust(140)[:140])
            sys.stdout.flush()
        else:
            print(line, flush=True)

    def close(self):
        self.render()
        if self.tty:
            sys.stdout.write("\n")
            sys.stdout.flush()


def fmt_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


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
    for row in todo:
        db.mark_op(conn, row["id"], "started")
        conn.commit()
        try:
            if row["kind"] == "rmdir":
                _lift_metadata(conn, run_id, row["src"])
            _do(row["kind"], row["src"], row["dst"], row["inode"])
            db.mark_op(conn, row["id"], "done")
            ok += 1
        except Exception as exc:                      # noqa: BLE001
            if row["kind"] == "rename" and _took_effect(row):
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
        return os.lstat(path).st_ino == inode
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
    the numbers are the ones the journal recorded: a network share can hand out
    new ones on every mount, and `stale` says that it has.
    """
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    if bool(row["is_dir"]) != stat.S_ISDIR(st.st_mode):
        return False
    if row["size"] and st.st_size != row["size"]:
        return False
    return stale or not row["inode"] or st.st_ino == row["inode"]


def inodes_are_stale(rows) -> bool:
    """Whether the inode numbers in the journal are from another mount.

    Looks at where the run's files are by name and size. When they are there and
    most do not carry the recorded number, the share has numbered its files anew
    since the run, and the numbers prove nothing either way.
    """
    found = matched = 0
    for row in rows:
        if row["kind"] != "rename" or not row["inode"]:
            continue
        for path in (row["dst"], row["src"]):
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if row["size"] and st.st_size != row["size"]:
                continue
            found += 1
            matched += st.st_ino == row["inode"]
            break
        if found >= 200:
            break
    return found > 0 and matched * 2 < found


def _took_effect(row) -> bool:
    """A rename is proven done when its file is at the destination and gone from
    the source. Without a recorded inode nothing is proven."""
    return bool(row["inode"]) and not os.path.lexists(row["src"]) \
        and holds(row["dst"], row["inode"])


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
        op_id = db.append_op(conn, run_id, "rename", src, dst, size=info.st_size,
                             inode=info.st_ino, status="started", reason=METADATA_MOVE)
        conn.commit()
        _do("rename", src, dst, info.st_ino)
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
        actual = os.lstat(src).st_ino
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
