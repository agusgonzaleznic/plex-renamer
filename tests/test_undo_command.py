"""The undo promises, through the command line and its run bookkeeping."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from test_roundtrip import CATS, FIXTURE, LibraryCase, dirs, snapshot
from test_undo_safety import MOVIE, write

from plexrenamer import cli, db, executor

class UndoCommand(LibraryCase):
    def setUp(self):
        super().setUp()
        self.src = os.path.join(self.root, MOVIE)
        self.root_as_typed = self.root
        self.before, self.before_dirs = snapshot(self.root), dirs(self.root)

    def _cli(self, *args):
        out = io.StringIO()
        argv = ["--root", self.root_as_typed, "--db", self.dbpath, "--categories", *CATS,
                "--decisions", os.path.join(self.state, "none"), *args]
        with contextlib.redirect_stdout(out):
            try:
                code = cli.main(argv)
            except SystemExit as exc:
                code = exc.code
            except KeyboardInterrupt:
                code = "KeyboardInterrupt escaped"
        return code, out.getvalue()

    def _run_status(self, run_id=1):
        conn = db.connect(self.dbpath)
        try:
            return db.get_run(conn, run_id)["status"]
        finally:
            conn.close()

    def test_runs_are_undone_newest_first(self):
        self.assertEqual(self._cli("apply", "--rate", "5000")[0], 0)
        late = "Movies/Hollow Tide (2021) [1080p] [WEBRip] [YTS.MX]/Hollow.Tide.2021.1080p.mp4"
        os.makedirs(os.path.dirname(os.path.join(self.root, late)))
        write(os.path.join(self.root, late), "z" * 32)
        written = snapshot(self.root)[late]
        self.assertEqual(self._cli("apply", "--rate", "5000")[0], 0)
        applied = snapshot(self.root)
        code, out = self._cli("undo", "--run", "1")
        self.assertEqual(code, 1, out)
        self.assertIn("run 2", out)
        self.assertEqual(snapshot(self.root), applied, "an older run was undone first")
        for expected in ("undoing run 2", "undoing run 1"):
            code, out = self._cli("undo")
            self.assertEqual(code, 0, out)
            self.assertIn(expected, out)
        self.assertIn("nothing to undo", self._cli("undo")[1])
        after = snapshot(self.root)
        self.assertEqual(after.pop(late), written)
        self.assertEqual(after, self.before)
        self.assertEqual((self._run_status(1), self._run_status(2)), ("undone", "undone"))

    def test_undo_says_what_is_left_and_finishes_on_a_later_try(self):
        self.assertEqual(self._cli("apply", "--rate", "5000")[0], 0)
        write(self.src)
        for _attempt in (1, 2):                   # the second try must not give up either
            code, out = self._cli("undo")
            self.assertEqual(code, 1, out)
            self.assertIn("reoccupied", out)
            self.assertEqual(self._run_status(), "partially-undone")
        os.remove(self.src)
        code, out = self._cli("undo")
        self.assertEqual(code, 0, out)
        self.assertEqual(self._run_status(), "undone")
        self.assertEqual(snapshot(self.root), self.before)
        self.assertEqual(dirs(self.root), self.before_dirs)

    def test_undo_says_how_to_go_on_and_counts_what_it_does_not_list(self):
        self.assertEqual(self._cli("apply", "--rate", "5000")[0], 0)
        write(self.src)                           # blocks the film and the folder it is in
        with mock.patch.object(cli, "SHOWN", 1):
            code, out = self._cli("undo")
        self.assertEqual(code, 1, out)
        self.assertEqual(out.count("PlanError"), 1, "one row listed, the other counted")
        self.assertIn("... and 1 more", out)
        self.assertIn("--force", out)

    def test_undo_changes_nothing_while_the_library_is_away(self):
        self.assertEqual(self._cli("apply", "--rate", "5000")[0], 0)
        away = self.root + ".away"
        os.rename(self.root, away)
        try:
            code, out = self._cli("undo")
        finally:
            os.rename(away, self.root)
        self.assertEqual(code, 1, out)
        self.assertIn("stopped", out)
        self.assertEqual(self._run_status(), "applied")
        code, out = self._cli("undo")
        self.assertEqual(code, 0, out)
        self.assertEqual(snapshot(self.root), self.before)
        self.assertEqual(dirs(self.root), self.before_dirs)

    def test_ctrl_c_stops_cleanly_and_the_printed_command_reverses_it(self):
        real, calls = executor._do, {"n": 0}

        def do(*args):
            calls["n"] += 1
            if calls["n"] == 20:
                raise KeyboardInterrupt()
            real(*args)
        with mock.patch.object(executor, "_do", do):
            code, out = self._cli("apply", "--rate", "5000")
        self.assertEqual(code, 130)
        self.assertIn("undo --run 1", out)
        self.assertEqual(self._run_status(), "interrupted")
        self.assertEqual(self._cli("undo", "--run", "1")[0], 0)
        self.assertEqual(snapshot(self.root), self.before)
        self.assertEqual(dirs(self.root), self.before_dirs)

    def test_a_later_run_blocks_however_its_library_path_is_written(self):
        self.assertEqual(self._cli("apply", "--rate", "5000")[0], 0)
        late = os.path.join(self.root, "Movies", "Hollow Tide (2021) [1080p] [YTS.MX]")
        os.makedirs(late)
        write(os.path.join(late, "Hollow.Tide.2021.1080p.mp4"), "z" * 32)
        self.root_as_typed = self.root + os.sep   # the same library, written another way
        self.assertEqual(self._cli("apply", "--rate", "5000")[0], 0)
        self.root_as_typed = self.root
        code, out = self._cli("undo", "--run", "1")
        self.assertEqual(code, 1, out)
        self.assertIn("run 2", out)

    def test_a_run_on_another_library_does_not_block(self):
        self.assertEqual(self._cli("apply", "--rate", "5000")[0], 0)
        other = tempfile.mkdtemp(prefix="plexrenamer-other-")
        try:
            for rel, payload in FIXTURE.items():
                path = os.path.join(other, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                write(path, payload * 32)
            self.root_as_typed = other
            self.assertEqual(self._cli("apply", "--rate", "5000")[0], 0)
            self.root_as_typed = self.root
            code, out = self._cli("undo", "--run", "1")
            self.assertEqual(code, 0, out)
            self.assertEqual(snapshot(self.root), self.before)
            self.assertEqual(dirs(self.root), self.before_dirs)
        finally:
            shutil.rmtree(other, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
