"""End to end: build a fake library, plan it, apply it, undo it, and prove the
tree comes back byte for byte identical. The fixture and the helpers here are
shared with test_recovery.py and test_undo_safety.py."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plexrenamer import classify, db, decisions, executor, naming, scan, undo  # noqa: E402
from plexrenamer.planner import Planner  # noqa: E402

FIXTURE = {
    # every title and id in this fixture is made up
    "Movies/Lanternfall (2025) [1080p] [WEBRip] [x265] [YTS.MX]/Lanternfall.2025.1080p.WEBRip.x265-[YTS.MX].mp4": "a",
    "Movies/Lanternfall (2025) [1080p] [WEBRip] [x265] [YTS.MX]/Subs/English.srt": "b",
    "Movies/Lanternfall (2025) [1080p] [WEBRip] [x265] [YTS.MX]/Subs/SDH eng HI.srt": "c",
    "Movies/Lanternfall (2025) [1080p] [WEBRip] [x265] [YTS.MX]/YTS.MX - Official site.jpg": "d",
    "Movies/The Quillhaven Collection/Quillhaven (1999)/Quillhaven (1999).mkv": "e",
    "Movies/The Quillhaven Collection/Quillhaven (1999)/Featurettes/Building Quillhaven.mkv": "f",
    "Movies/The Quillhaven Collection/Quillhaven Rising (2003)/Quillhaven Rising (2003).mkv": "g",
    "Movies/Loose Movie (1988).mkv": "h",
    "Movies/Split Movie (2001)/Split Movie (2001) - part1.mkv": "i",
    "Movies/Split Movie (2001)/Split Movie (2001) - part2.mkv": "j",
    "Series/Pebble Ridge/Season 01/Pebble Ridge S01E01.mp4": "k",
    "Series/Pebble Ridge/Season 01/Pebble Ridge S01E02.mp4": "l",
    "Series/Wickerby/Season 01/Wickerby 1x01 The First One.mkv": "m",
    "Series/Wickerby/Season 01/Wickerby 1x01 The First One.srt": "n",
    "Series/Wickerby/DS Store": "o",
    "Series/Moss.Atlas.2023.S01.1080p.H265-TBK/Moss.Atlas.2023.S01E01.1080p.H265-TBK.mkv": "p",
    "Classes/Bob - MasterClass - X - Medbay/740-01 - Introduction - Bob Teaches X.mp4": "q",
    "Classes/Bob - MasterClass - X - Medbay/740-02 - Second Lesson - Bob Teaches X.mp4": "r",
}
CATS = ["Movies", "Series", "Classes"]


def snapshot(root):
    out = {}
    for base, dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(base, name)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, root)] = hashlib.sha256(fh.read()).hexdigest()
    return out


def contents(root):
    """Map content hash -> set of relative paths, so we can follow a file's identity."""
    out = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(base, name)
            with open(path, "rb") as fh:
                out.setdefault(hashlib.sha256(fh.read()).hexdigest(), set()).add(
                    os.path.relpath(path, root))
    return out


def dirs(root):
    """Every directory below root, to catch a stray or a missing folder after undo."""
    return {os.path.relpath(os.path.join(base, name), root)
            for base, names, _files in os.walk(root) for name in names}


def remount(root):
    """Swap the tree for an identical copy: the same names and contents, every
    inode number new. That is how a network share comes back after a remount."""
    copy = root + ".remounted"
    shutil.copytree(root, copy, symlinks=True)
    shutil.rmtree(root)
    os.rename(copy, root)


class SwapMove:
    """The smallest thing build_ops accepts as a move."""

    def __init__(self, src, dst, with_inode=False):
        self.src, self.dst = src, dst
        self.is_dir, self.size = False, 0
        self.inode = db.inode_of(os.lstat(src)) if with_inode else 0
        self.category, self.unit, self.reason = "", "", "swap"


class LibraryCase(unittest.TestCase):
    """A throwaway library on disk and a throwaway journal."""

    EXTRA = {}

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="plexrenamer-test-")
        for rel, payload in {**FIXTURE, **self.EXTRA}.items():
            path = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(payload * 32)
        self.state = tempfile.mkdtemp(prefix="plexrenamer-state-")
        self.dbpath = os.path.join(self.state, "state.db")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.state, ignore_errors=True)

    def _boundaries(self):
        return [os.path.join(self.root, c) for c in CATS]

    def _plan(self, overrides=None):
        trees = scan.scan(self.root, CATS)
        units = []
        for cat in CATS:
            units.extend(classify.classify_category(cat, trees[cat]))
        units, _, _ = decisions.apply_to(units, overrides or {})
        planner = Planner(self.root)
        plans = [planner.plan_unit(u) for u in units]
        return units, [m for p in plans for m in p.moves]

    def _journal(self):
        """Plan the library and write the plan to a fresh journal."""
        _units, moves = self._plan()
        conn = db.connect(self.dbpath)
        run_id = db.create_run(conn, self.root, CATS, "test")
        db.insert_ops(conn, run_id, executor.build_ops(moves, self._boundaries()))
        return conn, run_id


class RoundTrip(LibraryCase):
    def test_plan_has_no_collisions(self):
        _units, moves = self._plan()
        self.assertEqual(executor.validate(moves), [])
        self.assertGreater(len(moves), 10)

    def test_apply_then_undo_restores_every_byte(self):
        before = snapshot(self.root)
        before_files = set(before)
        _units, moves = self._plan()
        self.assertEqual(executor.validate(moves), [])
        ops = executor.build_ops(moves, self._boundaries())
        conn = db.connect(self.dbpath)
        run_id = db.create_run(conn, self.root, CATS, "test")
        db.insert_ops(conn, run_id, ops)
        ok, failed = executor.apply_ops(conn, run_id, quiet=True)
        self.assertEqual(failed, 0, "apply reported failures")

        after = snapshot(self.root)
        self.assertNotEqual(before_files, set(after), "nothing was renamed")
        self.assertEqual(sorted(before.values()), sorted(after.values()),
                         "file contents changed or a file was lost")

        good, bad, problems = executor.verify_run(conn, run_id)
        self.assertEqual(bad, 0, problems)
        self.assertGreater(good, 0)

        ok, failed = undo.undo_run(conn, run_id, quiet=True)
        self.assertEqual(failed, 0, "undo reported failures")
        self.assertEqual(snapshot(self.root), before,
                         "undo did not restore the original tree exactly")

    def test_expected_destinations(self):
        _units, moves = self._plan()
        dst = {os.path.relpath(m.dst, self.root) for m in moves}
        for expected in [
            "Movies/Lanternfall (2025)/Lanternfall (2025).mp4",
            "Movies/Lanternfall (2025)/Lanternfall (2025).en.srt",
            "Movies/Lanternfall (2025)/Lanternfall (2025).en.sdh.srt",
            "Movies/Lanternfall (2025)/.YTS.MX - Official site.jpg",
            "Movies/Quillhaven (1999)/Quillhaven (1999).mkv",
            "Movies/Quillhaven (1999)/Featurettes/Building Quillhaven.mkv",
            "Movies/Quillhaven Rising (2003)/Quillhaven Rising (2003).mkv",
            "Movies/Loose Movie (1988)/Loose Movie (1988).mkv",
            "Series/Pebble Ridge/Season 01/Pebble Ridge - S01E01.mp4",
            "Series/Wickerby/Season 01/Wickerby - S01E01 - The First One.mkv",
            "Series/Wickerby/Season 01/Wickerby - S01E01 - The First One.en.srt",
            "Series/Moss Atlas (2023)/Season 01/Moss Atlas (2023) - S01E01.mkv",
            "Classes/Bob - MasterClass - X/Season 01/Bob - MasterClass - X - S01E01 - Introduction.mp4",
        ]:
            self.assertIn(expected, dst, f"missing planned destination: {expected}")

    def test_decisions_override_title_year_and_id(self):
        overrides = {
            "Series/Pebble Ridge": {"title": "Pebble Ridge", "year": 2014,
                                    "ids": {"imdb": "tt0000000"}},
        }
        _units, moves = self._plan(overrides)
        dst = {os.path.relpath(m.dst, self.root) for m in moves}
        self.assertIn(
            "Series/Pebble Ridge (2014) {imdb-tt0000000}/Season 01/"
            "Pebble Ridge (2014) - S01E01.mp4", dst)

    def test_hide_decision_keeps_every_file_out_of_sight(self):
        overrides = {"Movies/The Quillhaven Collection/Quillhaven (1999)": {"hide": True}}
        _units, moves = self._plan(overrides)
        hidden = [m for m in moves
                  if "/.The Quillhaven Collection/Quillhaven (1999)/" in m.dst]
        self.assertEqual(len(hidden), 2, "both files should move under the dot folder")
        for m in hidden:
            self.assertIn("/.The Quillhaven Collection/", m.dst)
        self.assertEqual(executor.validate(moves), [])

    def test_lint_is_clean_for_the_fixture(self):
        from plexrenamer import lint
        _units, moves = self._plan()
        issues = lint.check(moves, self.root, CATS)
        self.assertEqual(dict(issues), {}, f"lint findings: {dict(issues)}")

    def test_prune_never_climbs_above_a_category(self):
        _units, moves = self._plan()
        ops = executor.build_ops(moves, self._boundaries())
        for op in ops:
            if op.kind != "rmdir":
                continue
            self.assertTrue(
                any(op.src.startswith(b + os.sep) for b in self._boundaries()),
                f"rmdir escaped the library: {op.src}")

    def test_nothing_is_ever_deleted(self):
        before = contents(self.root)
        _units, moves = self._plan()
        ops = executor.build_ops(moves, self._boundaries())
        conn = db.connect(self.dbpath)
        run_id = db.create_run(conn, self.root, CATS, "test")
        db.insert_ops(conn, run_id, ops)
        executor.apply_ops(conn, run_id, quiet=True)
        after = contents(self.root)
        self.assertEqual(set(before), set(after),
                         "a distinct file content disappeared from the volume")
        for digest, paths in before.items():
            self.assertEqual(len(paths), len(after[digest]),
                             f"file count changed for content {digest[:8]}")

    def test_cycle_is_broken_with_a_temp_name(self):
        base = os.path.join(self.root, "cycle")
        os.makedirs(base)
        for name in ("a.mkv", "b.mkv"):
            with open(os.path.join(base, name), "w") as fh:
                fh.write(name)

        moves = [SwapMove(os.path.join(base, "a.mkv"), os.path.join(base, "b.mkv")),
                 SwapMove(os.path.join(base, "b.mkv"), os.path.join(base, "a.mkv"))]
        ops = executor.build_ops(moves, [base])
        self.assertEqual(sum(1 for o in ops if executor.TEMP_SUFFIX in (o.dst or "")), 1)
        conn = db.connect(self.dbpath)
        run_id = db.create_run(conn, self.root, ["cycle"], "cycle")
        db.insert_ops(conn, run_id, ops)
        ok, failed = executor.apply_ops(conn, run_id, quiet=True)
        self.assertEqual(failed, 0)
        with open(os.path.join(base, "a.mkv")) as fh:
            self.assertEqual(fh.read(), "b.mkv")
        with open(os.path.join(base, "b.mkv")) as fh:
            self.assertEqual(fh.read(), "a.mkv")
        _good, bad, problems = executor.verify_run(conn, run_id)
        self.assertEqual(bad, 0, problems)


class NamingRules(unittest.TestCase):
    def test_movie_patterns(self):
        info = naming.TitleInfo("movie", "Quillhaven", 1999, {"imdb": "tt0000000"})
        self.assertEqual(info.folder(), "Quillhaven (1999) {imdb-tt0000000}")
        self.assertEqual(naming.movie_file(info, ".mkv"),
                         "Quillhaven (1999) {imdb-tt0000000}.mkv")
        self.assertEqual(naming.movie_file(info, ".mkv", part=2),
                         "Quillhaven (1999) {imdb-tt0000000} - part2.mkv")

    def test_edition_tag_is_last(self):
        info = naming.TitleInfo("movie", "Blade Runner", 1982, {"imdb": "tt0083658"},
                                ["Final Cut"])
        self.assertEqual(naming.movie_file(info, ".mkv"),
                         "Blade Runner (1982) {imdb-tt0083658} {edition-Final Cut}.mkv")

    def test_episode_patterns(self):
        info = naming.TitleInfo("show", "Grey's Anatomy", 2005, {"tvdb": "73244"})
        self.assertEqual(info.folder(), "Grey's Anatomy (2005) {tvdb-73244}")
        self.assertEqual(naming.episode_file(info, 1, 2, ".mkv", ep_title="The First Cut"),
                         "Grey's Anatomy (2005) - S01E02 - The First Cut.mkv")
        self.assertEqual(naming.episode_file(info, 2, 1, ".avi", episode_end=3),
                         "Grey's Anatomy (2005) - S02E01-E03.avi")
        self.assertEqual(naming.season_folder(0), "Specials")
        self.assertEqual(naming.season_folder(7), "Season 07")

    def test_subtitle_patterns(self):
        self.assertEqual(naming.subtitle_file("Avatar (2009)", "en"), "Avatar (2009).en.srt")
        self.assertEqual(naming.subtitle_file("Avatar (2009)", "en", forced=True, ext=".ass"),
                         "Avatar (2009).en.forced.ass")
        self.assertEqual(naming.subtitle_file("Avatar (2009)", "de", sdh=True),
                         "Avatar (2009).de.sdh.srt")

    def test_illegal_characters_are_removed(self):
        info = naming.TitleInfo("movie", "Face/Off: The Movie", 1997)
        self.assertEqual(info.folder(), "Face-Off - The Movie (1997)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
