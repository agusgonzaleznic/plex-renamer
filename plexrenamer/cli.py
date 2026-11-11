"""Command line entry point."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

from . import classify, db, decisions, executor, lint, naming, scan, undo
from .planner import Planner

CATEGORIES = ["Animated Movies", "Animated Series", "Anime Movies", "Anime Series",
              "Classes", "Documentaries", "DocuSeries", "Movies", "Music Shows",
              "Series", "StandUp", "Theatre"]
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(HERE, "state", "plex-renamer.db")
DEFAULT_DECISIONS = os.path.join(HERE, "decisions")
REPORTS = os.path.join(HERE, "reports")


def build(root, categories, decisions_dir):
    trees = scan.scan(root, categories)
    missing = [c for c in categories if c not in trees]
    if missing:
        raise SystemExit(f"category directories not found under {root}: {missing}")
    units = []
    for cat in categories:
        units.extend(classify.classify_category(cat, trees[cat]))
    overrides = decisions.load(decisions_dir) if decisions_dir else {}
    units, changed, unmatched = decisions.apply_to(units, overrides)
    planner = Planner(root)
    plans = [planner.plan_unit(u) for u in units]
    return units, plans, changed, unmatched


def all_moves(plans):
    return [m for p in plans for m in p.moves]


def all_prunes(plans):
    return [d for p in plans for d in p.prunes]


def plan_hash(moves):
    h = hashlib.sha256()
    for m in sorted(moves, key=lambda m: (m.src, m.dst)):
        h.update(m.src.encode() + b"\0" + m.dst.encode() + b"\0")
    return h.hexdigest()[:16]


def write_reports(units, plans, moves, tag="plan"):
    os.makedirs(REPORTS, exist_ok=True)
    tsv = os.path.join(REPORTS, f"{tag}.tsv")
    with open(tsv, "w") as fh:
        fh.write("category\tunit\treason\tsrc\tdst\n")
        for p in plans:
            for m in p.moves:
                fh.write(f"{m.category}\t{m.unit}\t{m.reason}\t{m.src}\t{m.dst}\n")
    review = os.path.join(REPORTS, f"{tag}-units.json")
    with open(review, "w") as fh:
        json.dump([{
            "unit": u.src_rel, "category": u.category, "kind": u.kind,
            "title": u.info.title, "year": u.info.year, "ids": u.info.ids,
            "editions": u.info.editions, "flags": u.flags, "notes": u.notes,
            "dest": p.dest_dir[len(os.path.dirname(p.dest_dir.rsplit("/", 2)[0])) + 1:],
            "moves": len(p.moves),
        } for u, p in zip(units, plans)], fh, indent=1)
    return tsv, review


def summarise(units, plans, moves):
    from collections import Counter
    per_cat = Counter(m.category for m in moves)
    flags = Counter(f for u in units for f in u.flags)
    print(f"units: {len(units)}   file moves: {len(moves)}")
    for cat in sorted(per_cat):
        print(f"  {cat:18} {per_cat[cat]:5} moves")
    if flags:
        print("flags: " + ", ".join(f"{k}={v}" for k, v in flags.most_common()))


def cmd_plan(args):
    units, plans, changed, unmatched = build(args.root, args.categories,
                                             args.decisions)
    moves = all_moves(plans)
    summarise(units, plans, moves)
    if changed:
        print(f"decisions applied to {changed} units")
    if unmatched:
        print(f"WARNING {len(unmatched)} decision keys matched no unit:")
        for key in unmatched[:20]:
            print(f"  {key}")
    problems = executor.validate(moves)
    if problems:
        print(f"\n{len(problems)} plan problems:")
        for p in problems[:40]:
            print("  " + p)
    print()
    findings = lint.report(lint.check(moves, args.root, args.categories))
    tsv, review = write_reports(units, plans, moves, args.tag)
    print(f"\nplan written to {tsv}\nunit review written to {review}")
    return 1 if (problems or findings) else 0


def run_ops(conn, run_id, label):
    """Apply a journalled run. Ctrl-C stops it cleanly and says how to reverse it."""
    db.set_run_status(conn, run_id, "applying")
    try:
        ok, failed = executor.apply_ops(conn, run_id, label=label)
    except KeyboardInterrupt:
        db.set_run_status(conn, run_id, "interrupted", finished=True)
        print(f"\n\ninterrupted. What ran so far is in the journal; reverse it with:"
              f"  python3 -m plexrenamer undo --run {run_id}")
        raise SystemExit(130) from None
    db.set_run_status(conn, run_id, "applied" if not failed else "applied-with-errors",
                      finished=True)
    return ok, failed


def cmd_apply(args):
    units, plans, _, unmatched = build(args.root, args.categories, args.decisions)
    moves = all_moves(plans)
    if not moves:
        print("nothing to do")
        return 0
    problems = executor.validate(moves)
    if problems and not args.force:
        print(f"refusing to run: {len(problems)} plan problems")
        for p in problems[:40]:
            print("  " + p)
        return 1
    findings = lint.check(moves, args.root, args.categories)
    blocking = {k: v for k, v in findings.items()
                if k in {"destination outside its category", "duplicate destination",
                         "empty name segment", "unparseable \' (N)\' suffix"}}
    if blocking and not args.force:
        print("refusing to run: blocking lint findings")
        lint.report(blocking)
        return 1
    boundaries = [os.path.join(args.root, c) for c in args.categories]
    ops = executor.build_ops(moves, boundaries, all_prunes(plans))
    rate = args.rate or executor.benchmark(args.root)
    eta = len(ops) / rate
    from collections import Counter
    kinds = Counter(o.kind for o in ops)
    print(f"volume        : {args.root}")
    print(f"categories    : {len(args.categories)}")
    print(f"units         : {len(units)}")
    print(f"operations    : {len(ops)}  ({', '.join(f'{k}={v}' for k, v in kinds.items())})")
    print(f"measured rate : {rate:.0f} ops/sec on this filesystem")
    print(f"ESTIMATED TIME: {executor.fmt_duration(eta)}")
    if args.dry_run:
        print("dry run, nothing changed")
        return 0
    conn = db.connect(args.db)
    run_id = db.create_run(conn, args.root, args.categories, plan_hash(moves),
                           args.note)
    db.insert_ops(conn, run_id, ops)
    print(f"run id        : {run_id}\n")
    ok, failed = run_ops(conn, run_id, "renaming")
    print(f"\ndone: {ok} operations succeeded, {failed} failed")
    good, bad, verify_problems = executor.verify_run(conn, run_id)
    print(f"verify: {good} confirmed on disk, {bad} mismatched")
    for p in verify_problems[:20]:
        print("  " + p)
    if failed:
        for row in db.run_ops(conn, run_id):
            if row["status"] == "failed":
                print(f"  FAILED {row['kind']} {row['src']} -> {row['dst']}: {row['error']}")
    print(f"\nreverse this run with:  python3 -m plexrenamer undo --run {run_id}")
    return 0 if not failed else 1


def cmd_undo(args):
    conn = db.connect(args.db)
    run_id = args.run
    if run_id is None:
        run_id = db.newest_standing_run(conn)
        if run_id is None:
            print("nothing to undo")
            return 0
    run = db.get_run(conn, run_id)
    if run is None:
        print(f"no run {run_id}")
        return 1
    later = db.standing_runs_after(conn, run_id, run["root"])
    if later and not args.force:
        # a later run may have moved the same files again, and undoing this one
        # first would leave both half reversed
        print(f"run {run_id} is not the newest: run {', '.join(map(str, later))} "
              f"came after it and still stand{'s' if len(later) == 1 else ''}.\n"
              f"Undo the newest first (plain `undo` picks it), or pass --force.")
        return 1
    rows = [r for r in db.run_ops(conn, run_id) if r["status"] != "undone"]
    expected = [r for r in rows if r["status"] in undo.EXPECTED]
    print(f"undoing run {run_id}: {len(expected)} operations to reverse, "
          f"{len(rows) - len(expected)} more to check against the disk")
    if args.dry_run:
        for r in expected[:40]:
            print(f"  {r['kind']}  {r['dst']} -> {r['src']}")
        return 0
    try:
        ok, failed = undo.undo_run(conn, run_id)
    except undo.VolumeGone as exc:
        if any(r["status"] == "undone" for r in db.run_ops(conn, run_id)):
            db.set_run_status(conn, run_id, "partially-undone")
        print(f"\nstopped: {exc}\nNothing is marked as undone that is not. "
              f"Run undo again once the library is there.")
        return 1
    db.set_run_status(conn, run_id, "undone" if not failed else "partially-undone",
                      finished=True)
    print(f"\nundone: {ok} reversed, {failed} failed")
    if failed:
        notes = [r for r in db.run_ops(conn, run_id)
                 if r["status"] != "undone" and (r["error"] or "").startswith("undo:")]
        for r in notes[:20]:
            print(f"  {r['kind']} {r['dst'] or r['src']}: {r['error'][6:]}")
        print("Rows that failed keep their state: fix the cause and run undo again.")
    return 0 if not failed else 1


def media_free_dirs(root, categories, protected):
    """Directories inside the categories with no media anywhere below them."""
    from . import util
    keep_ext = (util.VIDEO_EXT | util.SUBTITLE_EXT | util.AUDIO_EXT |
                util.IMAGE_EXT | util.DOC_EXT | util.SIDECAR_EXT)
    out = []
    for category in categories:
        base = os.path.join(root, category)
        for current, dirs, files in os.walk(base, topdown=False):
            if current == base or current in protected:
                continue
            if any(os.path.splitext(f)[1].lower() in keep_ext for f in files):
                continue
            if any(os.path.join(current, d) not in out for d in dirs):
                continue
            real = [f for f in files
                    if not executor.METADATA_RE.fullmatch(f)
                    and not f.startswith("._")]
            if real:
                continue
            out.append(current)
    return out


def cmd_prune(args):
    units, plans, _, _ = build(args.root, args.categories, args.decisions)
    protected = set()
    for p in plans:
        # only the parents of real destinations are protected. A unit's dest_dir
        # on its own is not: for a grouping shell whose contents all moved out it
        # is exactly the directory that should go, and a genuinely empty title
        # folder is already safe because this tool never emptied it.
        for target in [os.path.dirname(m.dst) for m in p.moves]:
            d = target
            while d and d.startswith(args.root):
                protected.add(d)
                d = os.path.dirname(d)
    conn_read = db.connect(args.db)
    emptied = set()
    for row in conn_read.execute(
            "SELECT src FROM op WHERE kind='rename' AND status='done'"):
        d = os.path.dirname(row["src"] or "")
        while d and d.startswith(args.root):
            emptied.add(d)
            d = os.path.dirname(d)
    stale = [d for d in media_free_dirs(args.root, args.categories, protected)
             if d in emptied]
    skipped = [d for d in media_free_dirs(args.root, args.categories, protected)
               if d not in emptied]
    if skipped:
        print(f"{len(skipped)} media-free directories were never emptied by this "
              f"tool, so they are left alone:")
        for d in skipped:
            print("  " + d[len(args.root) + 1:])
        print()
    if not stale:
        print("no stale directories")
        return 0
    print(f"{len(stale)} directories hold no media and are not a planned destination:")
    for d in stale:
        print("  " + d[len(args.root) + 1:])
    if args.dry_run:
        print("\ndry run, nothing changed")
        return 0
    ops = [executor.Op(i + 1, "rmdir", d, None, True, 0, 0, "", "stale shell")
           for i, d in enumerate(stale)]
    conn = db.connect(args.db)
    run_id = db.create_run(conn, args.root, args.categories, "prune",
                           args.note or "prune stale directory shells")
    db.insert_ops(conn, run_id, ops)
    ok, failed = run_ops(conn, run_id, "pruning")
    print(f"\nremoved {ok} directory shells, {failed} failed")
    for row in db.run_ops(conn, run_id):
        if row["status"] == "failed":
            print(f"  kept {row['src']}: {row['error']}")
    print(f"\nreverse with:  python3 -m plexrenamer undo --run {run_id}")
    return 0


def cmd_status(args):
    conn = db.connect(args.db)
    rows = conn.execute("SELECT * FROM run ORDER BY id DESC LIMIT 20").fetchall()
    if not rows:
        print("no runs recorded")
        return 0
    for r in rows:
        counts = dict(conn.execute(
            "SELECT status, COUNT(*) FROM op WHERE run_id=? GROUP BY status",
            (r["id"],)).fetchall())
        print(f"run {r['id']:3}  {r['status']:22} {r['started_at']}  "
              f"plan={r['plan_hash']}  {counts}")
    return 0


def cmd_verify(args):
    conn = db.connect(args.db)
    run_id = args.run or (db.latest_run(conn) or {"id": None})["id"]
    if run_id is None:
        print("no runs recorded")
        return 1
    good, bad, problems = executor.verify_run(conn, run_id)
    print(f"run {run_id}: {good} confirmed on disk, {bad} mismatched")
    for p in problems[:60]:
        print("  " + p)
    return 0 if bad == 0 else 1


def main(argv=None):
    ap = argparse.ArgumentParser("plexrenamer", description=__doc__)
    ap.add_argument("--root", default="/Volumes/Multimedia")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--decisions", default=DEFAULT_DECISIONS)
    ap.add_argument("--categories", nargs="*", default=CATEGORIES)
    ap.add_argument("--episode-case", choices=["upper", "lower"], default="upper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan"); p.add_argument("--tag", default="plan"); p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("apply")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--rate", type=float, default=None)
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_apply)
    p = sub.add_parser("undo"); p.add_argument("--run", type=int)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true"); p.set_defaults(fn=cmd_undo)
    p = sub.add_parser("prune")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_prune)
    p = sub.add_parser("status"); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("verify"); p.add_argument("--run", type=int); p.set_defaults(fn=cmd_verify)

    args = ap.parse_args(argv)
    naming.EPISODE_CASE = args.episode_case
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
