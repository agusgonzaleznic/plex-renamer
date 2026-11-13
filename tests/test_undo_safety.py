"""Undo must never guess, never overwrite, and never give up on a row.

Each test builds one way a real library goes wrong: the share is not mounted, a
path is taken by somebody else's file, a reply from the share is lost, a run is
undone out of order. Every foreign file must survive untouched, and every row
that could not be undone must still be there for the next try."""

from __future__ import annotations

import errno
import os
import shutil
import unittest
from unittest import mock

from test_recovery import Interrupt
from test_roundtrip import CATS, LibraryCase, dirs, remount, snapshot

from plexrenamer import db, executor, undo

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

        def settle(*args):
            calls["n"] += 1
            result = real(*args)
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

    def test_a_missing_category_stops_undo_before_anything_is_touched(self):
        for category in CATS:
            self.tearDown()
            self.setUp()
            conn, run_id = self._applied()
            there, away = os.path.join(self.root, category), self.root + ".category"
            os.rename(there, away)
            try:
                with self.assertRaises(undo.VolumeGone):
                    undo.undo_run(conn, run_id, quiet=True)
            finally:
                os.rename(away, there)
            self.assertEqual({r["status"] for r in db.run_ops(conn, run_id)}, {"done"},
                             f"rows were undone although {category} was missing")
            conn.close()

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

    def test_apply_refuses_a_source_of_the_same_size_that_is_another_file(self):
        conn, run_id = self._journal()
        os.rename(self.src, self.src + ".old")
        write(self.src, "s" * 32)                 # same size as the film, another file
        _ok, failed = executor.apply_ops(conn, run_id, quiet=True)
        self.assertEqual(failed, 1)
        self.assertEqual(read(self.src), "s" * 32, "a file the plan never saw was moved")
        self.assertIn("inode changed", self._movie_row(conn, run_id)["error"])

    def test_apply_refuses_a_source_that_has_changed_size(self):
        conn, run_id = self._journal()
        with open(self.src, "a") as fh:
            fh.write("still being written")       # the same file, grown since the plan
        _ok, failed = executor.apply_ops(conn, run_id, quiet=True)
        self.assertEqual(failed, 1)
        self.assertEqual(read(self.src), "h" * 32 + "still being written")
        self.assertFalse(os.path.lexists(self.dst), "undo could not have found it there")
        self.assertIn("size changed", self._movie_row(conn, run_id)["error"])

    def test_apply_refuses_a_source_that_has_become_a_folder(self):
        conn, run_id = self._journal()
        os.rename(self.src, self.src + ".old")
        os.mkdir(self.src)
        _ok, failed = executor.apply_ops(conn, run_id, quiet=True)
        self.assertEqual(failed, 1)
        self.assertTrue(os.path.isdir(self.src), "a folder was moved in place of the film")
        self.assertIn("no longer a file", self._movie_row(conn, run_id)["error"])

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

    def test_on_one_mount_a_stranger_of_the_same_size_is_not_taken_back(self):
        conn, run_id = self._cut_at_the_movie(after=False)
        os.rename(self.src, os.path.join(self.root, "Movies", "moved by hand.mkv"))
        write(self.dst, "s" * 32)                 # same size as the film, another file
        _ok, failed = undo.undo_run(conn, run_id, quiet=True)
        self.assertGreaterEqual(failed, 1)
        self.assertFalse(os.path.lexists(self.src), "the inode said it was another file")
        self.assertEqual(read(self.dst), "s" * 32)

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

    def test_after_a_remount_an_unrelated_file_is_still_not_taken_back(self):
        conn, run_id = self._cut_at_the_movie(after=False)
        conn.close()
        remount(self.root)                        # the inode numbers are gone
        conn = db.connect(self.dbpath)
        os.rename(self.src, os.path.join(self.root, "Movies", "moved by hand.mkv"))
        write(self.dst)                           # another size, so another file
        _ok, failed = undo.undo_run(conn, run_id, quiet=True)
        self.assertGreaterEqual(failed, 1)
        self.assertFalse(os.path.lexists(self.src), "an unrelated file was moved back")
        self.assertEqual(read(self.dst), FOREIGN)

    # -- folders the run did not make or remove -------------------------------

    def test_a_run_that_stopped_at_its_first_step_leaves_other_folders_alone(self):
        conn, run_id = self._journal()
        with mock.patch.object(executor, "_do", side_effect=Interrupt()), \
                self.assertRaises(Interrupt):
            executor.apply_ops(conn, run_id, quiet=True)
        rows = db.run_ops(conn, run_id)
        planned_folder = [r["dst"] for r in rows if r["kind"] == "mkdir"][-1]
        pruned_folder = next(r["src"] for r in rows if r["kind"] == "rmdir")
        os.makedirs(planned_folder)               # made by hand, not by the run
        shutil.move(pruned_folder, self.root + ".moved-away")
        try:
            self.assertEqual(undo.undo_run(conn, run_id, quiet=True)[1], 0)
            self.assertTrue(os.path.isdir(planned_folder), "a hand-made folder was removed")
            self.assertFalse(os.path.lexists(pruned_folder),
                             "a folder the run never removed was recreated")
        finally:
            shutil.rmtree(self.root + ".moved-away", ignore_errors=True)

    def test_a_created_folder_on_record_as_failed_is_still_reported_and_removed(self):
        conn, run_id = self._applied()
        created = os.path.dirname(self.dst)
        conn.execute("UPDATE op SET status='failed' WHERE run_id=? AND kind='mkdir' AND dst=?",
                     (run_id, created))
        conn.commit()                             # the share reported an error for a mkdir that worked
        extra = os.path.join(created, "notes.txt")
        write(extra)
        self.assertEqual(undo.undo_run(conn, run_id, quiet=True)[1], 1,
                         "a folder the run made is still there, and that must be said")
        self.assertEqual(read(extra), FOREIGN)
        os.remove(extra)
        self._assert_restored(conn, run_id)

    def test_a_folder_whose_removal_is_not_on_record_comes_back(self):
        for lost in ("failed", "pending"):        # an error was reported, or the mark was lost
            self.tearDown()
            self.setUp()
            first, second = (os.path.join(self.root, "Movies", name)
                             for name in ("shell one", "shell two"))
            os.makedirs(first)
            os.makedirs(second)
            wanted = dirs(self.root)
            conn = db.connect(self.dbpath)
            run_id = db.create_run(conn, self.root, CATS, "prune")
            db.insert_ops(conn, run_id, [
                executor.Op(n, "rmdir", path, None, True, 0, 0, "", "stale shell")
                for n, path in enumerate((first, second), 1)])
            self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (2, 0))
            conn.execute("UPDATE op SET status=? WHERE run_id=? AND seq=1", (lost, run_id))
            conn.commit()
            self.assertEqual(undo.undo_run(conn, run_id, quiet=True)[1], 0)
            self.assertEqual(dirs(self.root), wanted, f"removal recorded as {lost}")
            conn.close()

    def test_a_lookup_that_fails_once_does_not_write_a_folder_off(self):
        conn, run_id = self._applied()
        created = os.path.dirname(self.dst)
        hiccups = {"n": 0}

        def once(real):
            # The first lookup of the folder fails, whichever call makes it.
            def look(path, *args, **kwargs):
                if path == created and not hiccups["n"]:
                    hiccups["n"] += 1
                    raise OSError(errno.EIO, "the share hiccuped")
                return real(path, *args, **kwargs)
            return look
        with mock.patch.object(os, "lstat", once(os.lstat)), \
                mock.patch.object(os, "stat", once(os.stat)):
            _ok, failed = undo.undo_run(conn, run_id, quiet=True)
        self.assertEqual((failed, hiccups["n"]), (1, 1))
        self.assertTrue(os.path.isdir(created))
        row = conn.execute("SELECT * FROM op WHERE run_id=? AND kind='mkdir' AND dst=?",
                           (run_id, created)).fetchone()
        self.assertEqual(row["status"], "done", "a folder that is still there was written off")
        self.assertIn("hiccuped", row["error"])
        self._assert_restored(conn, run_id)

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

    def test_a_row_not_on_record_as_done_does_not_take_back_a_reoccupied_path(self):
        # The move went through, the journal says it failed (a lost reply) or
        # never got its mark, and since then the old name is taken again.
        for status in ("failed", "pending"):
            self.tearDown()
            self.setUp()
            conn, run_id = self._applied()
            conn.execute("UPDATE op SET status=? WHERE id=?",
                         (status, self._movie_row(conn, run_id)["id"]))
            conn.commit()
            write(self.src)
            _ok, failed = undo.undo_run(conn, run_id, quiet=True)
            self.assertEqual(failed, 2, f"{status}: the rename, and the folder it could not leave")
            self.assertEqual((read(self.src), read(self.dst)), (FOREIGN, "h" * 32), status)
            row = self._movie_row(conn, run_id)
            self.assertEqual(row["status"], status, "a row that failed must stay to be retried")
            self.assertIn("reoccupied", row["error"], status)
            os.remove(self.src)
            self._assert_restored(conn, run_id)
            conn.close()


class MetadataNames(LibraryCase):
    """A file the plan itself moves to a metadata name is a file, not metadata."""

    SCENE = "Series/Moss.Atlas.2023.S01.1080p.H265-TBK"
    EXTRA = {f"{SCENE}/DS Store": "y"}

    def test_it_is_not_lifted_out_while_its_own_undo_is_blocked(self):
        before, before_dirs = snapshot(self.root), dirs(self.root)
        conn, run_id = self._journal()
        self.assertEqual(executor.apply_ops(conn, run_id, quiet=True)[1], 0)
        src = os.path.join(self.root, self.SCENE, "DS Store")
        row = conn.execute("SELECT * FROM op WHERE run_id=? AND src=?", (run_id, src)).fetchone()
        self.assertEqual(os.path.basename(row["dst"]), ".DS_Store")
        os.makedirs(os.path.dirname(src))         # the old folder is made again by hand
        write(src)                                # and its old name is taken
        _ok, failed = undo.undo_run(conn, run_id, quiet=True)
        self.assertGreaterEqual(failed, 2, "the file, and the folder it is still in")
        self.assertEqual(read(row["dst"]), "y" * 32, "it was moved somewhere else")
        self.assertEqual(read(src), FOREIGN)
        os.remove(src)
        self.assertEqual(undo.undo_run(conn, run_id, quiet=True)[1], 0)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(dirs(self.root), before_dirs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
