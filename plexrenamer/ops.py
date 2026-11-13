"""Expand a plan into the flat, ordered, reversible list of operations.

Ordering rules:
  1. every destination directory is created first (recorded, so undo can remove it)
  2. file moves run in an order that never overwrites a file that still has to move
  3. genuine cycles are broken with a temporary name, recorded as two moves
  4. directories the run empties are removed last, deepest first, and recreated on undo
"""

from __future__ import annotations

import os
from dataclasses import dataclass

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
