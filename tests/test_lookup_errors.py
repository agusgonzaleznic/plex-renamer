"""A lookup that fails is never read as a path that is free or gone.

On a network share a lookup can fail for a moment. os.path.lexists and
os.path.isdir read every such failure as "not there", and a rename onto a path
that is not there replaces whatever is there. Each test makes one lookup fail
where the tool decides something by it, checks that the operation in hand stops
and says so, and that a later try finishes the job.

The last tests are about removing folders: a system can remove a folder
together with files it regards as disposable, so the tool lists a folder first
and keeps one that has a "._" file of its name beside it."""

from __future__ import annotations

import contextlib
import errno
import os
import shlex
import sqlite3
import sys
import unittest
from unittest import mock

import test_undo_command
from test_lookalikes import Move
from test_review_gaps import hiccup
from test_roundtrip import CATS, LibraryCase, dirs, snapshot
from test_undo_safety import FOREIGN, MOVIE, MOVIE_DST, read, write

from plexrenamer import cli, db, executor, undo


@contextlib.contextmanager
def unreadable(path):
    """Every lookup of path fails while the block runs. The dict counts them."""
    real, seen = os.lstat, {"hits": 0}

    def lstat(p, *args, **kwargs):
        if os.fspath(p) == path:
            seen["hits"] += 1
            raise OSError(errno.EIO, "the share did not answer")
        return real(p, *args, **kwargs)
    with mock.patch.object(os, "lstat", lstat):
        yield seen


@contextlib.contextmanager
def disposable_hidden_files():
    """rmdir as macOS does it: a folder that holds only "._" files is removed,
    and the files with it."""
    real = os.rmdir

    def rmdir(path, *args, **kwargs):
        names = os.listdir(path)
        if names and all(name.startswith("._") for name in names):
            for name in names:
                os.remove(os.path.join(path, name))
        return real(path, *args, **kwargs)
    with mock.patch.object(os, "rmdir", rmdir):
        yield


@contextlib.contextmanager
def attributes_beside():
    """rmdir as macOS does it on a volume that keeps a folder's attributes in a
    "._" file beside the folder, such as FAT: that file goes with the folder."""
    real = os.rmdir

    def rmdir(path, *args, **kwargs):
        result = real(path, *args, **kwargs)
        beside = os.path.join(os.path.dirname(path), "._" + os.path.basename(path))
        if os.path.lexists(beside):
            os.remove(beside)
        return result
    with mock.patch.object(os, "rmdir", rmdir):
        yield


class _Library(LibraryCase):
    _cli = test_undo_command.UndoCommand._cli
    _run_status = test_undo_command.UndoCommand._run_status

    def setUp(self):
        super().setUp()
        self.root_as_typed = self.root
        self.src = os.path.join(self.root, MOVIE)
        self.dst = os.path.join(self.root, MOVIE_DST)
        self.parents = os.path.join(self.root, "Movies", ".DS_Store")
        self.before, self.before_dirs = snapshot(self.root), dirs(self.root)

    def _assert_restored(self, conn, run_id):
        self.assertEqual(undo.undo_run(conn, run_id, quiet=True)[1], 0)
        self.assertEqual(snapshot(self.root), self.before)
        self.assertEqual(dirs(self.root), self.before_dirs)

    def _move_out_of_a_folder(self, child_metadata):
        """A film moves out of a folder of its own into a new one, next to the
        parent's own folder metadata. The old folder is removed by the run."""
        write(self.parents, "the parent's own metadata")
        old = os.path.join(self.root, "Movies", "old")
        os.makedirs(old)
        write(os.path.join(old, "a.mkv"), "a film")
        if child_metadata:
            write(os.path.join(old, ".DS_Store"), "the old folder's metadata")
        self.before, self.before_dirs = snapshot(self.root), dirs(self.root)
        new = os.path.join(self.root, "Movies", "New (2000)")
        conn = db.connect(self.dbpath)
        run_id = db.create_run(conn, self.root, CATS, "move out")
        moves = [Move(os.path.join(old, "a.mkv"), os.path.join(new, "a.mkv"))]
        db.insert_ops(conn, run_id, executor.build_ops(moves, self._boundaries()))
        self.assertEqual([r["kind"] for r in db.run_ops(conn, run_id)],
                         ["mkdir", "rename", "rmdir"])
        return conn, run_id, old, new


class ApplyLookups(_Library):
    def test_a_destination_that_cannot_be_read_is_never_renamed_onto(self):
        movies = os.path.join(self.root, "Movies")
        a, b, c = (os.path.join(movies, f"{name}.mkv") for name in "abc")
        write(a, "the film a")
        write(b, "the film b, which is longer")
        conn = db.connect(self.dbpath)
        run_id = db.create_run(conn, self.root, CATS, "chain")
        db.insert_ops(conn, run_id, executor.build_ops([Move(a, b), Move(b, c)], self._boundaries()))
        write(c, FOREIGN)                         # somebody else's file, after the plan
        with hiccup(b, executor, "_do", lambda kind, src, dst, inode: src == a) as seen:
            self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (0, 2))
        self.assertEqual(seen["hits"], 1)
        rows = db.run_ops(conn, run_id)
        self.assertIn("destination occupied", rows[0]["error"])
        self.assertIn("the share hiccuped", rows[1]["error"])
        self.assertEqual((read(a), read(b), read(c)),
                         ("the film a", "the film b, which is longer", FOREIGN))

    def test_a_mark_that_cannot_be_written_after_a_rename_stops_the_run(self):
        # The rename went through and only its mark failed. On record as not
        # tried, undo would leave the file where it is and call the run undone.
        a, b = (os.path.join(self.root, "Movies", f"{name}.mkv") for name in "ab")
        write(a, "the film a")
        conn = db.connect(self.dbpath)
        run_id = db.create_run(conn, self.root, CATS, "one")
        db.insert_ops(conn, run_id, executor.build_ops([Move(a, b)], self._boundaries()))
        real, seen = db.mark_op, []

        def mark(conn_, op_id, status, *args, **kwargs):
            if status == "done" and not seen:
                seen.append(op_id)
                raise sqlite3.OperationalError("disk I/O error")
            return real(conn_, op_id, status, *args, **kwargs)
        with mock.patch.object(db, "mark_op", mark), \
                self.assertRaises(sqlite3.OperationalError):
            executor.apply_ops(conn, run_id, quiet=True)
        self.assertEqual(([r["status"] for r in db.run_ops(conn, run_id)], read(b)),
                         (["started"], "the film a"))
        self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (1, 0))
        self.assertEqual((read(a), os.path.lexists(b)), ("the film a", False))
        conn.close()

    def test_folder_metadata_never_replaces_the_parents_whose_name_cannot_be_read(self):
        conn, run_id, old, _new = self._move_out_of_a_folder(child_metadata=True)
        with unreadable(self.parents) as seen:
            self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (2, 1))
        self.assertGreater(seen["hits"], 0)
        self.assertEqual(read(self.parents), "the parent's own metadata")
        self.assertEqual(read(os.path.join(old, ".DS_Store")), "the old folder's metadata")
        self.assertIn("did not answer", db.run_ops(conn, run_id)[2]["error"])
        self._assert_restored(conn, run_id)

    def test_a_folder_that_cannot_be_read_before_its_metadata_moves_up_says_so(self):
        conn, run_id, old, _new = self._move_out_of_a_folder(child_metadata=True)
        with hiccup(old, executor, "_lift_metadata") as seen:
            self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (2, 1))
        rows = db.run_ops(conn, run_id)
        self.assertEqual((seen["hits"], len(rows)), (1, 3), "nothing moved up")
        self.assertIn("hiccuped", rows[2]["error"], "reported as a folder that holds something")
        self.assertEqual(read(os.path.join(old, ".DS_Store")), "the old folder's metadata")
        self._assert_restored(conn, run_id)

    def test_a_source_that_cannot_be_read_right_before_its_rename_stays(self):
        # It has shrunk since the plan, and the look that would show it fails. Read
        # as not there, the rename would go ahead, and undo could not find the file.
        to = os.path.join(self.root, "Movies", "renamed.mkv")
        conn = db.connect(self.dbpath)
        run_id = db.create_run(conn, self.root, CATS, "one")
        db.insert_ops(conn, run_id, executor.build_ops([Move(self.src, to)], self._boundaries()))
        with open(self.src, "r+") as fh:
            fh.truncate(5)
        with hiccup(self.src, executor, "_refuse_changed") as seen:
            self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (0, 1))
        error = db.run_ops(conn, run_id)[0]["error"]
        self.assertEqual(seen["hits"], 1)
        self.assertTrue(error.startswith(executor.NOT_TRIED) and "hiccuped" in error, error)
        self.assertEqual((read(self.src), os.path.lexists(to)), ("h" * 5, False))
        self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (0, 0))
        self.assertEqual(read(self.src), "h" * 5)

    def test_a_lost_reply_whose_outcome_cannot_be_read_is_on_record_as_failed(self):
        conn, run_id = self._journal()
        real = os.rename

        def lossy(src, dst, *args, **kwargs):
            real(src, dst, *args, **kwargs)
            if src == self.src:
                raise OSError(errno.EIO, "the reply was lost")
        with mock.patch.object(os, "rename", lossy), \
                hiccup(self.src, executor, "_took_effect") as seen:
            executor.apply_ops(conn, run_id, quiet=True)
        self.assertEqual(seen["hits"], 1)
        row = next(r for r in db.run_ops(conn, run_id) if r["src"] == self.src)
        self.assertEqual(row["status"], "failed")
        self.assertIn("whether it took effect could not be read", row["error"])
        self.assertEqual(read(self.dst), "h" * 32, "the rename did go through")
        self._assert_restored(conn, run_id)

    def test_a_refused_rename_is_never_asked_whether_it_went_through(self):
        conn, run_id = self._journal()
        os.makedirs(os.path.dirname(self.dst))
        write(self.dst, "s" * 32)                 # a file of the same size, after the plan
        asked = []
        real = executor._took_effect

        def took_effect(row, keeps):
            asked.append(row["src"])
            return real(row, keeps)
        with mock.patch.object(executor, "_took_effect", took_effect):
            executor.apply_ops(conn, run_id, quiet=True)
        row = next(r for r in db.run_ops(conn, run_id) if r["src"] == self.src)
        self.assertEqual(row["status"], "failed")
        self.assertIn("destination occupied", row["error"])
        self.assertNotIn(self.src, asked, "nothing was renamed, so there is nothing to ask")
        undo.undo_run(conn, run_id, quiet=True)
        self.assertEqual((read(self.src), read(self.dst)), ("h" * 32, "s" * 32))

    def test_a_folder_already_there_when_the_run_makes_it_is_left_as_it_was(self):
        # Made by someone else after the plan, or there all along and missed by
        # a lookup that failed while the plan was made. Either way not the run's.
        for how in ("made after the plan", "unreadable while planning"):
            self.tearDown()
            self.setUp()
            new = os.path.join(self.root, "Movies", "New (2000)")
            if how == "made after the plan":
                conn, run_id, old, _new = self._move_out_of_a_folder(child_metadata=False)
                os.makedirs(new)
                write(os.path.join(new, ".DS_Store"), "its own metadata")
            else:
                os.makedirs(new)
                write(os.path.join(new, ".DS_Store"), "its own metadata")
                real, hits = os.stat, []

                def stat(p, *args, **kwargs):
                    if os.fspath(p) == new and not hits:
                        hits.append(p)
                        raise OSError(errno.EIO, "the share did not answer")
                    return real(p, *args, **kwargs)
                with mock.patch.object(os, "stat", stat):
                    conn, run_id, old, _new = self._move_out_of_a_folder(child_metadata=False)
                self.assertEqual(len(hits), 1, "the lookup that failed")
            self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (3, 0), how)
            mkdir = db.run_ops(conn, run_id)[0]
            self.assertEqual(mkdir["status"], "undone", how)
            self.assertIn("not made by this run", mkdir["error"], how)
            self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (2, 0), how)
            self.assertEqual(read(os.path.join(new, ".DS_Store")), "its own metadata", how)
            self.assertEqual(read(os.path.join(old, "a.mkv")), "a film", how)
            self.assertFalse(os.path.lexists(self.parents + "-1"), how)
            conn.close()


class UndoLookups(_Library):
    def test_undo_never_replaces_the_parents_metadata_whose_name_cannot_be_read(self):
        conn, run_id, _old, new = self._move_out_of_a_folder(child_metadata=False)
        self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (3, 0))
        write(os.path.join(new, ".DS_Store"), "written by Finder after the run")
        with unreadable(self.parents) as seen:
            self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (2, 1))
        self.assertGreater(seen["hits"], 0)
        self.assertEqual(read(self.parents), "the parent's own metadata")
        self.assertEqual(read(os.path.join(new, ".DS_Store")), "written by Finder after the run")
        self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (1, 0))
        self.assertEqual(read(self.parents), "the parent's own metadata")
        self.assertEqual(read(self.parents + "-1"), "written by Finder after the run")
        self.assertFalse(os.path.lexists(new))

    def test_undo_stops_when_the_library_cannot_be_read_while_it_looks_the_run_over(self):
        self.assertEqual(self._cli("apply", "--rate", "5000")[0], 0)
        applied = snapshot(self.root)
        with hiccup(self.dst, undo, "_survey") as seen:
            code, out = self._cli("undo")
        self.assertEqual((code, seen["hits"]), (1, 1), out)
        self.assertIn("stopped: the library cannot be read right now", out)
        self.assertEqual(snapshot(self.root), applied, "something moved on half a look")
        self.assertEqual(self._run_status(), "applied")
        code, out = self._cli("undo")
        self.assertEqual(code, 0, out)
        self.assertEqual(snapshot(self.root), self.before)
        self.assertEqual(dirs(self.root), self.before_dirs)


class Verify(_Library):
    def test_verify_says_it_could_not_check_and_fails(self):
        self.assertEqual(self._cli("apply", "--rate", "5000")[0], 0)
        with hiccup(self.dst, executor, "verify_run") as seen:
            code, out = self._cli("verify")
        self.assertEqual((code, seen["hits"]), (1, 1), out)
        self.assertIn("not checked, the library cannot be read right now", out)
        self.assertIn(cli.command("verify", 1, self.dbpath), out)
        self.assertIn(f"--db {shlex.quote(self.dbpath)} verify --run 1", out)
        code, out = self._cli("verify")
        self.assertEqual(code, 0, out)
        self.assertIn(" 0 mismatched", out)

    def test_apply_fails_when_it_cannot_verify_what_it_did(self):
        with hiccup(self.dst, executor, "verify_run") as seen:
            code, out = self._cli("apply", "--rate", "5000")
        self.assertEqual((code, seen["hits"]), (1, 1), out)
        self.assertIn("verify: not checked", out)
        self.assertIn("reverse this run with:", out)
        self.assertEqual(self._run_status(), "applied", "the run itself went through")

    def test_one_row_that_cannot_be_read_counts_as_a_mismatch(self):
        conn, run_id = self._journal()
        self.assertEqual(executor.apply_ops(conn, run_id, quiet=True)[1], 0)
        dst = self.dst
        with hiccup(dst, executor, "_mismatch", lambda row, *_: row["dst"] == dst) as seen:
            _good, bad, problems = executor.verify_run(conn, run_id)
        self.assertEqual((bad, seen["hits"]), (1, 1))
        self.assertTrue(problems[0].startswith("cannot be read"), problems)
        self.assertEqual(executor.verify_run(conn, run_id)[1:], (0, []))


class RemovingFolders(_Library):
    @unittest.skipUnless(sys.platform == "darwin", "the behaviour shown is the one macOS has")
    def test_the_control_a_bare_rmdir_here_takes_hidden_files_along(self):
        shell = os.path.join(self.root, "Movies", "shell")
        os.makedirs(shell)
        write(os.path.join(shell, "._notes"), "the owner's notes")
        os.rmdir(shell)
        self.assertFalse(os.path.lexists(shell))

    def test_apply_never_removes_a_folder_that_holds_anything(self):
        shell = os.path.join(self.root, "Movies", "shell")
        notes = os.path.join(shell, "._notes")
        os.makedirs(shell)
        write(notes, "the owner's notes")
        conn = db.connect(self.dbpath)
        run_id = db.create_run(conn, self.root, CATS, "prune")
        db.insert_ops(conn, run_id, [executor.Op(1, "rmdir", shell, None, True, 0, 0, "", "shell")])
        with disposable_hidden_files():
            self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (0, 1))
        self.assertEqual(read(notes), "the owner's notes")
        self.assertIn("not empty, left in place", db.run_ops(conn, run_id)[0]["error"])

    def test_prune_keeps_a_folder_whose_only_file_is_hidden(self):
        shell = os.path.join(self.root, "Movies", "Gone Film (1990)")
        hidden = os.path.join(shell, "._.DS_Store")
        os.makedirs(shell)
        write(hidden, "kept for macOS by the share")
        conn = db.connect(self.dbpath)                # an earlier run emptied the folder
        run_id = db.create_run(conn, self.root, CATS, "earlier run")
        film = os.path.join(shell, "Gone Film (1990).mkv")
        db.insert_ops(conn, run_id, [executor.Op(1, "rename", film, film + ".moved", False, 1, 0,
                                                 "Movies", "earlier")])
        db.mark_op(conn, db.run_ops(conn, run_id)[0]["id"], "done")
        conn.commit()
        conn.close()
        with disposable_hidden_files():
            code, out = self._cli("prune")
        self.assertEqual(code, 0, out)
        self.assertIn("removed 0 directory shells, 1 failed", out)
        self.assertIn("not empty, left in place", out)
        self.assertEqual(read(hidden), "kept for macOS by the share")
        self.assertIn(f"--db {shlex.quote(self.dbpath)} undo --run 2", out)

    def test_the_control_rmdir_on_such_a_volume_takes_the_file_beside_along(self):
        shell = os.path.join(self.root, "Movies", "shell")
        beside = os.path.join(self.root, "Movies", "._shell")
        os.makedirs(shell)
        write(beside, "the shell's attributes")
        with attributes_beside():
            os.rmdir(shell)
        self.assertFalse(os.path.lexists(beside))

    def test_apply_keeps_a_folder_whose_attributes_sit_beside_it(self):
        movies = os.path.join(self.root, "Movies")
        empty, holding = os.path.join(movies, "empty"), os.path.join(movies, "holding")
        for shell in (empty, holding):
            os.makedirs(shell)
            write(os.path.join(movies, "._" + os.path.basename(shell)), "its attributes")
        write(os.path.join(holding, ".DS_Store"), "its own metadata")
        conn = db.connect(self.dbpath)
        run_id = db.create_run(conn, self.root, CATS, "prune")
        db.insert_ops(conn, run_id, [executor.Op(n, "rmdir", shell, None, True, 0, 0, "", "shell")
                                     for n, shell in enumerate((empty, holding), 1)])
        with attributes_beside():
            self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (0, 2))
        for name in ("._empty", "._holding"):
            self.assertEqual(read(os.path.join(movies, name)), "its attributes")
        self.assertTrue(os.path.isdir(empty))
        self.assertEqual(read(os.path.join(holding, ".DS_Store")), "its own metadata")
        rows = db.run_ops(conn, run_id)
        self.assertEqual(len(rows), 2, "nothing is lifted out of a folder that stays")
        self.assertIn("._empty beside it would go with it", rows[0]["error"])
        self.assertIn("._holding beside it would go with it", rows[1]["error"])

    def test_undo_keeps_a_created_folder_whose_attributes_sit_beside_it(self):
        conn, run_id, _old, new = self._move_out_of_a_folder(child_metadata=False)
        self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (3, 0))
        beside = os.path.join(os.path.dirname(new), "._" + os.path.basename(new))
        write(os.path.join(new, ".DS_Store"), "written by Finder after the run")
        write(beside, "written by Finder after the run, too")
        with attributes_beside():
            self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (2, 1))
        self.assertEqual(read(beside), "written by Finder after the run, too")
        self.assertEqual(read(os.path.join(new, ".DS_Store")), "written by Finder after the run")
        self.assertFalse(os.path.lexists(self.parents + "-1"), "nothing moves up for a folder that stays")
        mkdir = next(r for r in db.run_ops(conn, run_id) if r["kind"] == "mkdir")
        self.assertIn("beside it would go with it", mkdir["error"])

    def _pruned(self):
        """A run that removed an empty folder, shell."""
        shell = os.path.join(self.root, "Movies", "shell")
        os.makedirs(shell)
        conn = db.connect(self.dbpath)
        run_id = db.create_run(conn, self.root, CATS, "prune")
        db.insert_ops(conn, run_id, [executor.Op(1, "rmdir", shell, None, True, 0, 0, "", "shell")])
        return conn, run_id, shell

    def test_a_folder_whose_lookup_fails_as_it_is_removed_is_on_record_as_failed(self):
        conn, run_id, shell = self._pruned()
        with hiccup(shell, executor, "remove_empty") as seen:
            self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (0, 1))
        self.assertEqual(seen["hits"], 1)
        self.assertIn("hiccuped", db.run_ops(conn, run_id)[0]["error"])
        self.assertTrue(os.path.isdir(shell))

    def test_undo_never_reads_the_name_of_a_removed_folder_as_free_on_a_failed_look(self):
        # Undo asks whether the folder is there, then whether anything else is.
        # Read as free, a link to a folder at that name would pass for the folder.
        for nth in (1, 2):
            self.tearDown()
            self.setUp()
            conn, run_id, shell = self._pruned()
            self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (1, 0))
            with hiccup(shell, undo, "_settle", nth=nth) as seen:
                self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (0, 1), nth)
            self.assertEqual(seen["hits"], 1, nth)
            self.assertIn("hiccuped", db.run_ops(conn, run_id)[0]["error"], nth)
            self.assertFalse(os.path.lexists(shell), nth)
            self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (1, 0), nth)
            self.assertTrue(os.path.isdir(shell), nth)
            conn.close()

    def test_a_removed_folder_whose_name_a_file_has_taken_is_reported(self):
        conn, run_id, shell = self._pruned()
        self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (1, 0))
        write(shell)                                  # a file has its name since
        for _ in range(2):
            self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (0, 1))
            row = db.run_ops(conn, run_id)[0]
            self.assertEqual(row["status"], "done")
            self.assertIn("something else has its name", row["error"])
            self.assertEqual(read(shell), FOREIGN)
        os.remove(shell)
        self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (1, 0))
        self.assertTrue(os.path.isdir(shell))


class PrintedCommand(_Library):
    def test_the_printed_undo_command_names_the_journal_it_needs(self):
        self.assertEqual(cli.undo_command(4), "python3 -m plexrenamer undo --run 4")
        self.assertEqual(cli.undo_command(4, cli.DEFAULT_DB), "python3 -m plexrenamer undo --run 4")
        self.assertEqual(cli.undo_command(4, "/x/my journal.db"),
                         "python3 -m plexrenamer --db '/x/my journal.db' undo --run 4")
        code, out = self._cli("apply", "--rate", "5000")
        self.assertEqual(code, 0, out)
        printed = out.split("reverse this run with:", 1)[1].strip().splitlines()[0]
        self.assertEqual(printed, cli.undo_command(1, self.dbpath))
        argv = shlex.split(printed)[3:]
        self.assertEqual(argv[:2], ["--db", self.dbpath])
        # Run as printed. The library to reverse comes from the journal.
        with contextlib.redirect_stdout(open(os.devnull, "w")):
            self.assertEqual(cli.main(["--root", self.root, *argv]), 0)
        self.assertEqual(snapshot(self.root), self.before)
        self.assertEqual(dirs(self.root), self.before_dirs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
