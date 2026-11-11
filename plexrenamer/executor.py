"""Apply and reverse a plan.

Ordering rules:
  1. every destination directory is created first (recorded, so undo can remove it)
  2. file moves run in an order that never overwrites a file that still has to move
  3. genuine cycles are broken with a temporary name, recorded as two moves
  4. directories we emptied are removed last, deepest first, and recreated on undo

Nothing is ever deleted except a directory this tool itself emptied.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass

from . import db

TEMP_SUFFIX = ".plexrenamer-tmp"


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
                      0, victim.category, victim.reason))

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
    rows = db.run_ops(conn, run_id)
    todo = [r for r in rows if r["status"] == "pending"]
    bar = None if quiet else Progress(len(todo), label)
    ok = failed = 0
    for row in todo:
        try:
            _do(row["kind"], row["src"], row["dst"], row["inode"])
            db.mark_op(conn, row["id"], "done")
            ok += 1
        except Exception as exc:                      # noqa: BLE001
            db.mark_op(conn, row["id"], "failed", f"{type(exc).__name__}: {exc}")
            failed += 1
        if ok % 200 == 0:
            conn.commit()
        if bar:
            bar.step(1, os.path.basename(row["dst"] or row["src"] or ""))
    conn.commit()
    if bar:
        bar.close()
    return ok, failed


def _do(kind: str, src, dst, inode):
    if kind == "mkdir":
        os.makedirs(dst, exist_ok=True)
        return
    if kind == "rmdir":
        if not os.path.isdir(src):
            return
        leftovers = [e for e in os.listdir(src)
                     if re.fullmatch(r"(?:\.DS_Store|Thumbs\.db)(?:-\d+)?", e)]
        for name in leftovers:
            target = os.path.join(os.path.dirname(src), name)
            n = 0
            while os.path.lexists(target):
                n += 1
                target = os.path.join(os.path.dirname(src), f"{name}-{n}")
            os.rename(os.path.join(src, name), target)
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


def undo_run(conn, run_id: int, quiet: bool = False):
    rows = [r for r in db.run_ops(conn, run_id, reverse=True) if r["status"] == "done"]
    bar = None if quiet else Progress(len(rows), "undo")
    ok = failed = 0
    for row in rows:
        try:
            if row["kind"] == "rename":
                if not os.path.lexists(row["dst"]):
                    raise FileNotFoundError(row["dst"])
                if os.path.lexists(row["src"]):
                    raise PlanError(f"original path reoccupied: {row['src']}")
                os.makedirs(os.path.dirname(row["src"]), exist_ok=True)
                os.rename(row["dst"], row["src"])
            elif row["kind"] == "mkdir":
                if os.path.isdir(row["dst"]) and not [
                        e for e in os.listdir(row["dst"]) if e != ".DS_Store"]:
                    shutil.rmtree(row["dst"], ignore_errors=True)
            elif row["kind"] == "rmdir":
                os.makedirs(row["src"], exist_ok=True)
            db.mark_op(conn, row["id"], "undone", undone=True)
            ok += 1
        except Exception as exc:                      # noqa: BLE001
            db.mark_op(conn, row["id"], "undo-failed", f"{type(exc).__name__}: {exc}",
                       undone=True)
            failed += 1
        if ok % 200 == 0:
            conn.commit()
        if bar:
            bar.step(1)
    conn.commit()
    if bar:
        bar.close()
    return ok, failed


def verify_run(conn, run_id: int):
    """Confirm on-disk reality matches the journal."""
    good = bad = 0
    problems = []
    for row in db.run_ops(conn, run_id):
        if row["status"] != "done":
            continue
        if row["kind"] == "rename":
            if not os.path.lexists(row["dst"]):
                problems.append(f"missing destination: {row['dst']}")
                bad += 1
                continue
            if row["inode"] and os.lstat(row["dst"]).st_ino != row["inode"]:
                problems.append(f"inode mismatch at {row['dst']}")
                bad += 1
                continue
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
