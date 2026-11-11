"""Undo must never guess, never overwrite, and never give up on a row.

Each test builds one way a real library goes wrong: the share is not mounted, a
path is taken by somebody else's file, a reply from the share is lost, a run is
undone out of order. Every foreign file must survive untouched, and every row
that could not be undone must still be there for the next try."""

from __future__ import annotations

import contextlib
import errno
import io
import os
import unittest
from unittest import mock

from test_recovery import Interrupt
from test_roundtrip import CATS, LibraryCase, dirs, snapshot

from plexrenamer import cli, db, executor, undo

MOVIE = "Movies/Loose Movie (1988).mkv"
MOVIE_DST = "Movies/Loose Movie (1988)/Loose Movie (1988).mkv"
FOREIGN = "someone else's file"


def write(path, text=FOREIGN):
    with open(path, "w") as fh:
        fh.write(text)


def read(path):
    with open(path) as fh:
        return fh.read()


class UndoSafety(LibraryCase):
    def setUp(self):
        super().setUp()
        self.src = os.path.join(self.root, MOVIE)
        self.dst = os.path.join(self.root, MOVIE_DST)
        self.before, self.before_dirs = snapshot(self.root), dirs(self.root)

    def _applied(self):
        conn, run_id = self._journal()
        _ok, failed = executor.apply_ops(conn, run_id, quiet=True)
        self.assertEqual(failed, 0)
        return conn, run_id

    def _cut_at_the_movie(self, after):
        """Apply, cut off at the rename of MOVIE, before or right after it."""
        conn, run_id = self._journal()
        real = executor._do

        def do(kind, src, dst, inode):
            if src == self.src and not after:
                raise Interrupt()
            real(kind, src, dst, inode)
            if src == self.src and after:
                raise Interrupt()
        with mock.patch.object(executor, "_do", do), self.assertRaises(Interrupt):
            executor.apply_ops(conn, run_id, quiet=True)
        conn.close()                              # the process is gone
        return db.connect(self.dbpath), run_id

    def _movie_row(self, conn, run_id):
        return conn.execute("SELECT * FROM op WHERE run_id=? AND kind='rename' AND src=?",
                            (run_id, self.src)).fetchone()

    def _assert_restored(self, conn, run_id):
        self.assertEqual(undo.undo_run(conn, run_id, quiet=True)[1], 0)
        self.assertEqual(snapshot(self.root), self.before)
        self.assertEqual(dirs(self.root), self.before_dirs)

    # -- the library is not there -------------------------------------------

    def test_undo_refuses_while_the_library_is_not_there(self):
        conn, run_id = self._applied()
        away = self.root + ".away"
        os.rename(self.root, away)                # the share is unmounted
        try:
            with self.assertRaises(undo.VolumeGone):
                undo.undo_run(conn, run_id, quiet=True)
            os.mkdir(self.root)                   # a stale, empty mount point
            with self.assertRaises(undo.VolumeGone):
                undo.undo_run(conn, run_id, quiet=True)
        finally:
            os.rename(away, self.root)
        self.assertEqual({r["status"] for r in db.run_ops(conn, run_id)}, {"done"},
                         "a row was written off while the library was away")
        self._assert_restored(conn, run_id)

    def test_undo_stops_when_the_library_goes_away_part_way(self):
        conn, run_id = self._applied()
        away = self.root + ".away"
        real, calls = undo._settle, {"n": 0}

        def settle(row):
            calls["n"] += 1
            result = real(row)
            if calls["n"] == 12:
                os.rename(self.root, away)        # the share drops here
            return result
        with mock.patch.object(undo, "_settle", settle), \
                self.assertRaises(undo.VolumeGone):
            undo.undo_run(conn, run_id, quiet=True)
        os.rename(away, self.root)
        states = [r["status"] for r in db.run_ops(conn, run_id)]
        self.assertEqual(states.count("undone"), 12)
        self.assertEqual(set(states), {"done", "undone"})
        self._assert_restored(conn, run_id)

    # -- somebody else's file is in the way ----------------------------------

    def test_a_reoccupied_path_is_not_overwritten_and_the_row_is_tried_again(self):
        conn, run_id = self._applied()
        write(self.src)
        _ok, failed = undo.undo_run(conn, run_id, quiet=True)
        self.assertEqual(failed, 2, "the rename, and the folder it could not leave")
        self.assertEqual(read(self.src), FOREIGN)
        self.assertEqual(read(self.dst), "h" * 32)
        row = self._movie_row(conn, run_id)
        self.assertEqual(row["status"], "done", "a row that failed must stay to be retried")
        self.assertIn("reoccupied", row["error"])
        os.remove(self.src)
        self._assert_restored(conn, run_id)

    def test_apply_never_overwrites_a_destination_that_got_taken(self):
        conn, run_id = self._journal()
        real = executor._do

        def do(kind, src, dst, inode):
            if src == self.src:
                write(dst)                        # it appears just before the rename
            real(kind, src, dst, inode)
        with mock.patch.object(executor, "_do", do):
            _ok, failed = executor.apply_ops(conn, run_id, quiet=True)
        self.assertEqual(failed, 1)
        self.assertEqual(read(self.dst), FOREIGN)
        self.assertEqual(read(self.src), "h" * 32)
        self.assertIn("destination occupied", self._movie_row(conn, run_id)["error"])
        _ok, failed = undo.undo_run(conn, run_id, quiet=True)
        self.assertEqual(failed, 1, "the created folder is kept: a foreign file lives in it")
        self.assertEqual(read(self.dst), FOREIGN)
        self.assertEqual(read(self.src), "h" * 32)
        self.assertEqual(self._movie_row(conn, run_id)["status"], "failed")

    def test_apply_refuses_a_source_that_is_no_longer_the_planned_file(self):
        conn, run_id = self._journal()
        os.rename(self.src, self.src + ".old")
        write(self.src)
        _ok, failed = executor.apply_ops(conn, run_id, quiet=True)
        self.assertEqual(failed, 1)
        self.assertEqual(read(self.src), FOREIGN, "a file the plan never saw was moved")
        self.assertIn("inode changed", self._movie_row(conn, run_id)["error"])

    def test_a_created_folder_that_holds_other_files_is_kept(self):
        conn, run_id = self._applied()
        extra = os.path.join(os.path.dirname(self.dst), "notes.txt")
        write(extra)
        _ok, failed = undo.undo_run(conn, run_id, quiet=True)
        self.assertEqual(failed, 1)
        self.assertEqual(read(extra), FOREIGN)
        os.remove(extra)
        self._assert_restored(conn, run_id)

    def test_a_different_file_at_the_old_path_is_not_taken_for_the_moved_one(self):
        conn, run_id = self._applied()
        os.rename(self.dst, os.path.join(self.root, "Movies", "moved by hand.mkv"))
        write(self.src)
        _ok, failed = undo.undo_run(conn, run_id, quiet=True)
        self.assertEqual(failed, 1)
        row = self._movie_row(conn, run_id)
        self.assertEqual(row["status"], "done", "written off as already back")
        self.assertIn("cannot find", row["error"])
        self.assertEqual(read(self.src), FOREIGN)

    # -- a run that was cut off ----------------------------------------------

    def test_a_cut_off_move_is_not_written_off_when_its_old_path_is_taken(self):
        conn, run_id = self._cut_at_the_movie(after=True)
        self.assertEqual(self._movie_row(conn, run_id)["status"], "started")
        write(self.src)                           # a later run put a file here
        _ok, failed = undo.undo_run(conn, run_id, quiet=True)
        self.assertGreaterEqual(failed, 1)
        self.assertEqual(read(self.dst), "h" * 32)
        self.assertEqual(read(self.src), FOREIGN)
        self.assertEqual(self._movie_row(conn, run_id)["status"], "started")
        os.remove(self.src)
        self._assert_restored(conn, run_id)

    def test_a_cut_off_move_never_takes_another_file_back(self):
        conn, run_id = self._cut_at_the_movie(after=False)
        os.rename(self.src, os.path.join(self.root, "Movies", "moved by hand.mkv"))
        write(self.dst)
        _ok, failed = undo.undo_run(conn, run_id, quiet=True)
        self.assertGreaterEqual(failed, 1)
        self.assertFalse(os.path.lexists(self.src), "an unrelated file was moved back")
        self.assertEqual(read(self.dst), FOREIGN)

    def test_a_cut_off_move_onto_a_taken_name_is_left_alone(self):
        conn, run_id = self._journal()
        os.makedirs(os.path.dirname(self.dst))
        write(self.dst)
        with self.assertRaises(Interrupt):
            self._cut_and_apply_rows(conn, run_id)
        self.assertEqual(self._movie_row(conn, run_id)["status"], "started")
        undo.undo_run(conn, run_id, quiet=True)
        self.assertEqual(read(self.dst), FOREIGN)
        self.assertEqual(read(self.src), "h" * 32)
        self.assertEqual(self._movie_row(conn, run_id)["status"], "undone",
                         "the move never happened, so there is nothing left to undo")

    def test_without_an_inode_an_ambiguous_cut_off_move_is_not_guessed(self):
        base = os.path.join(self.root, "Movies")
        a, b = os.path.join(base, "a.mkv"), os.path.join(base, "b.mkv")
        write(a, "a.mkv")
        write(b, "b.mkv")
        conn = db.connect(self.dbpath)
        run_id = db.create_run(conn, self.root, CATS, "no inode on record")
        db.insert_ops(conn, run_id, [executor.Op(1, "rename", a, b, False, 0, 0, "", "x")])
        with mock.patch.object(executor, "_do", side_effect=Interrupt()), \
                self.assertRaises(Interrupt):
            executor.apply_ops(conn, run_id, quiet=True)
        self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (0, 1),
                         "both names exist and nothing says which file is which")
        self.assertEqual((read(a), read(b)), ("a.mkv", "b.mkv"))
        row = conn.execute("SELECT * FROM op WHERE run_id=?", (run_id,)).fetchone()
        self.assertEqual(row["status"], "started")

    def _cut_and_apply_rows(self, conn, run_id):
        real = executor._do

        def do(kind, src, dst, inode):
            if src == self.src:
                raise Interrupt()
            real(kind, src, dst, inode)
        with mock.patch.object(executor, "_do", do):
            executor.apply_ops(conn, run_id, quiet=True)

    # -- the share reported an error for a rename that went through ------------

    def test_a_rename_that_went_through_but_reported_an_error_is_done(self):
        conn, run_id = self._journal()
        real = os.rename

        def lossy(src, dst, *args, **kwargs):
            real(src, dst, *args, **kwargs)
            if src == self.src:
                raise OSError(errno.EIO, "the reply was lost")
        with mock.patch.object(executor.os, "rename", lossy):
            _ok, failed = executor.apply_ops(conn, run_id, quiet=True)
        self.assertEqual(failed, 0)
        row = self._movie_row(conn, run_id)
        self.assertEqual(row["status"], "done")
        self.assertIn("took effect", row["error"])
        self.assertEqual(executor.verify_run(conn, run_id)[1], 0)
        self._assert_restored(conn, run_id)

    def test_a_failed_row_that_happened_after_all_is_reversed(self):
        conn, run_id = self._applied()
        conn.execute("UPDATE op SET status='failed', error='OSError: the reply was lost'"
                     " WHERE id=?", (self._movie_row(conn, run_id)["id"],))
        conn.commit()
        self._assert_restored(conn, run_id)
        self.assertEqual(self._movie_row(conn, run_id)["status"], "undone")


class UndoCommand(LibraryCase):
    """The same promises, through the command line and its run bookkeeping."""

    def setUp(self):
        super().setUp()
        self.src = os.path.join(self.root, MOVIE)
        self.before, self.before_dirs = snapshot(self.root), dirs(self.root)

    def _cli(self, *args):
        out = io.StringIO()
        argv = ["--root", self.root, "--db", self.dbpath, "--categories", *CATS,
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
        self.assertEqual(after.pop(late), snapshot(self.root)[late])
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
