"""Promises the other suites leave open.

A lookup that fails once on a share, folder metadata that meets the parent's
own, the first rename of a run, a source that shrank, `undo --force` and the
order of runs, a run on path numbers that is longer than the stale check looks
at, plan validation, and the speed test that `apply` runs first."""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

import test_undo_command
import test_undo_safety
from test_recovery import Interrupt
from test_roundtrip import CATS, LibraryCase, SwapMove, dirs, snapshot
from test_share_numbers import by_path, share
from test_undo_safety import FOREIGN, MOVIE, MOVIE_DST, read, write

from plexrenamer import cli, db, executor, undo


@contextlib.contextmanager
def hiccup(path, module, step, when=lambda *args: True, nth=1):
    """The nth lookup of path (an lstat or a stat) made inside module.step fails
    with EIO, once. By default the first one does.

    Only a call of step that `when` accepts arms it. Every other lookup reads the
    disk. The yielded dict counts the hiccups, so a test can prove there was one.
    """
    real_step, real = getattr(module, step), {"lstat": os.lstat, "stat": os.stat}
    state = {"armed": False, "hits": 0, "seen": 0}

    def wrapped(*args):
        state["armed"], state["seen"] = not state["hits"] and when(*args), 0
        try:
            return real_step(*args)
        finally:
            state["armed"] = False

    def lookup(name):
        def look(p, *args, **kwargs):
            if state["armed"] and not isinstance(p, int) and os.fspath(p) == path:
                state["seen"] += 1
                if state["seen"] == nth:
                    state["armed"], state["hits"] = False, state["hits"] + 1
                    raise OSError(errno.EIO, "the share hiccuped")
            return real[name](p, *args, **kwargs)
        return look
    with mock.patch.object(module, step, wrapped), \
            mock.patch.object(os, "lstat", lookup("lstat")), \
            mock.patch.object(os, "stat", lookup("stat")):
        yield state


def on_row(src):
    """A `when` for hiccup() on undo._settle: only while the row moving src is settled."""
    return lambda row, _state: row["src"] == src


class LookupErrors(LibraryCase):
    """A lookup that fails is never read as a path that is free or gone."""

    def setUp(self):
        super().setUp()
        self.src = os.path.join(self.root, MOVIE)
        self.dst = os.path.join(self.root, MOVIE_DST)
        self.before, self.before_dirs = snapshot(self.root), dirs(self.root)

    def _row(self, conn, run_id, src):
        return conn.execute("SELECT * FROM op WHERE run_id=? AND kind='rename' AND src=?",
                            (run_id, src)).fetchone()

    def _assert_restored(self, conn, run_id):
        self.assertEqual(undo.undo_run(conn, run_id, quiet=True)[1], 0)
        self.assertEqual(snapshot(self.root), self.before)
        self.assertEqual(dirs(self.root), self.before_dirs)

    def test_a_failed_rename_whose_destination_cannot_be_read_is_tried_again(self):
        conn, run_id = self._journal()
        self.assertEqual(executor.apply_ops(conn, run_id, quiet=True)[1], 0)
        # renamed in place, so no folder the run made keeps the run from being undone
        src = os.path.join(self.root, "Series/Pebble Ridge/Season 01/Pebble Ridge S01E01.mp4")
        dst = os.path.join(os.path.dirname(src), "Pebble Ridge - S01E01.mp4")
        self.assertEqual(self._row(conn, run_id, src)["dst"], dst)
        conn.execute("UPDATE op SET status='failed', error='OSError: the reply was lost'"
                     " WHERE run_id=? AND src=?", (run_id, src))
        conn.commit()                             # the share reported an error for it
        with hiccup(dst, undo, "_settle", on_row(src)) as seen:
            _ok, failed = undo.undo_run(conn, run_id, quiet=True)
        self.assertEqual((failed, seen["hits"]), (1, 1))
        row = self._row(conn, run_id, src)
        self.assertEqual(row["status"], "failed", "a row that could not be read was dropped")
        self.assertIn("hiccuped", row["error"])
        self.assertEqual(read(dst), "k" * 32)
        self._assert_restored(conn, run_id)

    def test_a_foreign_file_at_the_old_path_survives_a_lookup_that_fails(self):
        conn, run_id = self._journal()
        self.assertEqual(executor.apply_ops(conn, run_id, quiet=True)[1], 0)
        write(self.src)                           # a later run put a file here
        with hiccup(self.src, undo, "_settle", on_row(self.src)) as seen:
            _ok, failed = undo.undo_run(conn, run_id, quiet=True)
        self.assertEqual((failed, seen["hits"]), (2, 1),
                         "the rename, and the folder it could not leave")
        self.assertEqual(read(self.src), FOREIGN, "somebody else's file was overwritten")
        self.assertEqual(read(self.dst), "h" * 32)
        self.assertEqual(self._row(conn, run_id, self.src)["status"], "done")
        os.remove(self.src)
        self._assert_restored(conn, run_id)

    def test_a_first_destination_that_cannot_be_read_keeps_the_inode_check_on(self):
        base = os.path.join(self.root, "Movies")
        films = [os.path.join(base, f"film {n}.mkv") for n in range(3)]
        for path in films:
            write(path, "f" * 40)
        ops = []
        for n, path in enumerate(films, 1):
            st = os.lstat(path)
            ops.append(executor.Op(n, "rename", path, path.replace(".mkv", " (2000).mkv"),
                                   False, st.st_size, db.inode_of(st), "Movies", "rename"))
        conn = db.connect(self.dbpath)
        run_id = db.create_run(conn, self.root, CATS, "three films")
        db.insert_ops(conn, run_id, ops)
        os.rename(films[1], films[1] + ".old")
        write(films[1], "s" * 40)                 # same size as the film, another file
        with hiccup(ops[0].dst, executor, "_kept_number") as seen:
            self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (2, 1))
        self.assertEqual(seen["hits"], 1)
        self.assertIn("inode changed", self._row(conn, run_id, films[1])["error"])
        self.assertEqual(read(films[1]), "s" * 40, "a file the plan never saw was moved")
        self.assertFalse(os.path.lexists(ops[1].dst))
        self.assertEqual(read(ops[2].dst), "f" * 40)

    def _one_rename(self):
        """An applied run of one rename: the film at self.src to a name beside it."""
        st, to = os.lstat(self.src), self.src.replace(".mkv", " (renamed).mkv")
        conn = db.connect(self.dbpath)
        run_id = db.create_run(conn, self.root, CATS, "one")
        db.insert_ops(conn, run_id, [executor.Op(1, "rename", self.src, to, False, st.st_size,
                                                 db.inode_of(st), "Movies", "rename")])
        self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (1, 0))
        return conn, run_id, to

    def test_undo_stops_when_the_old_path_of_a_lost_mark_cannot_be_read(self):
        # Read as free, that path would prove on half a look that the rename ran.
        conn, run_id, to = self._one_rename()
        conn.execute("UPDATE op SET status='pending' WHERE run_id=?", (run_id,))
        conn.commit()                             # the journal lost the mark
        with hiccup(self.src, undo, "_survey") as seen, self.assertRaises(undo.VolumeGone):
            undo.undo_run(conn, run_id, quiet=True)
        self.assertEqual((seen["hits"], read(to), os.path.lexists(self.src)), (1, "h" * 32, False))
        self._assert_restored(conn, run_id)

    def test_a_file_back_at_its_old_path_that_cannot_be_read_is_not_called_lost(self):
        conn, run_id, to = self._one_rename()
        os.rename(to, self.src)                   # moved back by hand
        with hiccup(self.src, undo, "_settle") as seen:
            self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (0, 1))
        note = db.run_ops(conn, run_id)[0]["error"]
        self.assertEqual(seen["hits"], 1)
        self.assertIn("hiccuped", note)
        self.assertNotIn("cannot find", note, "it could not be read, which is not lost")
        self._assert_restored(conn, run_id)


class ChangedSources(LibraryCase):
    """Apply moves only what the plan saw."""

    def test_the_first_rename_of_a_run_refuses_a_source_of_the_same_size(self):
        conn, run_id = self._journal()
        first = next(r for r in db.run_ops(conn, run_id) if r["kind"] == "rename")
        os.rename(first["src"], first["src"] + ".old")
        write(first["src"], "s" * first["size"])  # same size, another file
        _ok, failed = executor.apply_ops(conn, run_id, quiet=True)
        self.assertEqual(failed, 2, "the rename, and the folder it was to empty")
        row = conn.execute("SELECT * FROM op WHERE id=?", (first["id"],)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("inode changed", row["error"])
        self.assertEqual(read(first["src"]), "s" * first["size"],
                         "a file the plan never saw was moved")
        self.assertFalse(os.path.lexists(first["dst"]))

    def test_apply_refuses_a_source_that_has_shrunk(self):
        conn, run_id = self._journal()
        src, dst = os.path.join(self.root, MOVIE), os.path.join(self.root, MOVIE_DST)
        with open(src, "r+") as fh:
            fh.truncate(5)                        # the same file, rewritten smaller
        _ok, failed = executor.apply_ops(conn, run_id, quiet=True)
        self.assertEqual(failed, 1)
        self.assertEqual(read(src), "h" * 5)
        self.assertFalse(os.path.lexists(dst), "undo could not have found it there")
        row = conn.execute("SELECT * FROM op WHERE run_id=? AND src=?", (run_id, src)).fetchone()
        self.assertIn("size changed", row["error"])


class MetadataOnUndo(LibraryCase):
    """Folder metadata is never deleted or overwritten, and a file is not metadata."""

    SCENE = test_undo_safety.MetadataNames.SCENE
    EXTRA = test_undo_safety.MetadataNames.EXTRA

    def _journal(self):
        return test_undo_safety.journal_as_before(self)

    def test_metadata_moved_up_does_not_replace_the_parents_own(self):
        parents = os.path.join(self.root, "Movies", ".DS_Store")
        write(parents, "the parent's own metadata")
        before, before_dirs = snapshot(self.root), dirs(self.root)
        conn, run_id = self._journal()
        self.assertEqual(executor.apply_ops(conn, run_id, quiet=True)[1], 0)
        created = os.path.join(self.root, "Movies", "Lanternfall (2025)")
        write(os.path.join(created, ".DS_Store"), "finder wrote this later")
        self.assertEqual(undo.undo_run(conn, run_id, quiet=True)[1], 0)
        self.assertEqual(read(parents), "the parent's own metadata")
        self.assertEqual(read(parents + "-1"), "finder wrote this later")
        after = snapshot(self.root)
        del after[os.path.relpath(parents + "-1", self.root)]
        self.assertEqual(after, before)
        self.assertEqual(dirs(self.root), before_dirs)

    def test_a_cut_off_move_to_a_metadata_name_is_not_lifted_out(self):
        before, before_dirs = snapshot(self.root), dirs(self.root)
        conn, run_id = self._journal()
        src = os.path.join(self.root, self.SCENE, "DS Store")
        real = executor._do

        def do(kind, src_, dst, inode):
            real(kind, src_, dst, inode)
            if src_ == src:
                raise Interrupt()                 # cut right after the move
        with mock.patch.object(executor, "_do", do), self.assertRaises(Interrupt):
            executor.apply_ops(conn, run_id, quiet=True)
        conn.close()                              # the process is gone
        conn = db.connect(self.dbpath)
        row = conn.execute("SELECT * FROM op WHERE run_id=? AND src=?", (run_id, src)).fetchone()
        self.assertEqual((row["status"], os.path.basename(row["dst"])), ("started", ".DS_Store"))
        write(src)                                # its old name is taken
        _ok, failed = undo.undo_run(conn, run_id, quiet=True)
        self.assertGreaterEqual(failed, 2, "the file, and the folder it is still in")
        self.assertEqual(read(row["dst"]), "y" * 32, "it was moved somewhere else")
        self.assertEqual(read(src), FOREIGN)
        os.remove(src)
        self.assertEqual(undo.undo_run(conn, run_id, quiet=True)[1], 0)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(dirs(self.root), before_dirs)


class PruneCutAfterALift(LibraryCase):
    def test_a_folder_the_run_never_reached_is_not_recreated_after_a_lift(self):
        shells = [os.path.join(self.root, "Movies", f"shell {n}") for n in ("one", "two", "three")]
        for shell in shells:
            os.makedirs(shell)
        write(os.path.join(shells[0], ".DS_Store"), "x" * 32)
        conn = db.connect(self.dbpath)
        run_id = db.create_run(conn, self.root, CATS, "prune")
        db.insert_ops(conn, run_id, [
            executor.Op(n, "rmdir", path, None, True, 0, 0, "", "stale shell")
            for n, path in enumerate(shells, 1)])
        real = executor._do

        def do(kind, src, dst, inode):
            if kind == "rmdir" and src == shells[1]:
                raise Interrupt()
            real(kind, src, dst, inode)
        with mock.patch.object(executor, "_do", do), self.assertRaises(Interrupt):
            executor.apply_ops(conn, run_id, quiet=True)
        conn.close()                              # the process is gone
        conn = db.connect(self.dbpath)
        rows = [(r["seq"], r["kind"], r["status"]) for r in db.run_ops(conn, run_id)]
        self.assertEqual(rows, [(1, "rmdir", "done"), (2, "rmdir", "started"),
                                (3, "rmdir", "pending"), (4, "rename", "done")],
                         "the lift is journalled behind every planned row")
        os.rmdir(shells[2])                       # removed by somebody else
        self.assertEqual(undo.undo_run(conn, run_id, quiet=True)[1], 0)
        self.assertEqual(read(os.path.join(shells[0], ".DS_Store")), "x" * 32)
        self.assertTrue(os.path.isdir(shells[1]))
        self.assertFalse(os.path.lexists(shells[2]),
                         "a folder the run never got to was recreated")


class ForceUndo(LibraryCase):
    _cli = test_undo_command.UndoCommand._cli
    _run_status = test_undo_command.UndoCommand._run_status

    def setUp(self):
        super().setUp()
        self.root_as_typed = self.root
        self.before, self.before_dirs = snapshot(self.root), dirs(self.root)

    def _two_runs(self):
        self.assertEqual(self._cli("apply", "--rate", "5000")[0], 0)
        late = "Movies/Hollow Tide (2021) [1080p] [YTS.MX]/Hollow.Tide.2021.1080p.mp4"
        os.makedirs(os.path.dirname(os.path.join(self.root, late)))
        write(os.path.join(self.root, late), "z" * 32)
        written = snapshot(self.root)[late]
        self.assertEqual(self._cli("apply", "--rate", "5000")[0], 0)
        return late, written

    def test_the_refusal_names_the_run_to_undo_first(self):
        self._two_runs()
        conn = db.connect(self.dbpath)                # a newer run, on another library
        other = db.create_run(conn, self.root + "-elsewhere", CATS, "elsewhere")
        db.set_run_status(conn, other, "applied", finished=True)
        conn.close()
        code, out = self._cli("undo", "--run", "1")
        self.assertEqual(code, 1, out)
        self.assertIn("run 2 came after it and still stands", out)
        self.assertIn(f"(`{cli.undo_command(2, self.dbpath)}`)", out)
        self.assertEqual((self._run_status(1), self._run_status(2)), ("applied", "applied"))

    def test_force_undoes_an_older_run_while_a_later_one_stands(self):
        late, written = self._two_runs()
        code, out = self._cli("undo", "--run", "1", "--force")
        self.assertEqual(code, 0, out)
        self.assertEqual((self._run_status(1), self._run_status(2)), ("undone", "applied"))
        code, out = self._cli("undo")             # the later run, on its own
        self.assertEqual(code, 0, out)
        self.assertIn("undoing run 2", out)
        self.assertEqual(snapshot(self.root), {**self.before, late: written})
        self.assertEqual(dirs(self.root), self.before_dirs | {os.path.dirname(late)})


class LongRunOnPathNumbers(LibraryCase):
    """On a share that works numbers out from the path, a run longer than the
    sample the stale check takes is still applied, verified and undone."""

    def test_more_than_200_moved_files_are_verified_and_undone(self):
        base = os.path.join(self.root, "Movies", "bulk")
        os.makedirs(base)
        paths = [os.path.join(base, f"film {n:03d}.mkv") for n in range(250)]
        for n, path in enumerate(paths):
            write(path, "f" * (n + 1))            # tiny, and each of its own size
        before, before_dirs = snapshot(self.root), dirs(self.root)
        with share(by_path):
            ops = []
            for n, path in enumerate(paths, 1):
                st = executor.os.lstat(path)
                ops.append(executor.Op(n, "rename", path, path.replace(".mkv", " (2000).mkv"),
                                       False, st.st_size, db.inode_of(st), "Movies", "bulk"))
            conn = db.connect(self.dbpath)
            run_id = db.create_run(conn, self.root, CATS, "bulk")
            db.insert_ops(conn, run_id, ops)
            self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (250, 0))
            self.assertTrue(executor.inodes_are_stale(db.run_ops(conn, run_id)),
                            "every moved file has another number here")
            self.assertEqual(executor.verify_run(conn, run_id), (250, 0, []))
            self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (250, 0))
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(dirs(self.root), before_dirs)


class PlanValidation(LibraryCase):
    """A plan that could lose data does not start unless forced, and even then
    nothing is overwritten."""

    _cli = test_undo_command.UndoCommand._cli

    def setUp(self):
        super().setUp()
        self.root_as_typed = self.root
        movies = os.path.join(self.root, "Movies")
        self.path = lambda name: os.path.join(movies, name)
        self.part1 = self.path("Split Movie (2001)/Split Movie (2001) - part1.mkv")
        self.part2 = self.path("Split Movie (2001)/Split Movie (2001) - part2.mkv")
        self.before = snapshot(self.root)

    def test_every_kind_of_problem_is_found(self):
        loose, gone = self.path("Loose Movie (1988).mkv"), self.path("gone.mkv")
        film = self.path("The Quillhaven Collection/Quillhaven (1999)/Quillhaven (1999).mkv")
        one = self.path("One (2000).mkv")
        problems = executor.validate([
            SwapMove(loose, one), SwapMove(film, one),           # one destination, two sources
            SwapMove(loose, self.path("Two (2000).mkv")),        # a source listed twice
            SwapMove(gone, self.path("Three (2000).mkv")),       # a source that is not there
            SwapMove(self.part2, self.part1)])                    # a destination that is taken
        self.assertEqual(problems, [
            f"source listed twice: {loose}",
            f"collision: {loose} + {film}  ->  {one}",
            f"source vanished: {gone}",
            f"destination already occupied: {self.part1}"])

    def test_apply_refuses_such_a_plan_and_forced_overwrites_nothing(self):
        plan = types.SimpleNamespace(moves=[SwapMove(self.part2, self.part1)], prunes=[])
        with mock.patch.object(cli, "build", lambda *args: ([], [plan], 0, [])):
            code, out = self._cli("apply", "--rate", "5000")
            self.assertEqual(code, 1, out)
            self.assertIn("refusing to run: 1 plan problems", out)
            self.assertIn(f"destination already occupied: {self.part1}", out)
            self.assertFalse(os.path.exists(self.dbpath), "no run was started")
            code, out = self._cli("apply", "--rate", "5000", "--force")
        self.assertEqual(code, 1, out)
        self.assertIn(f"destination occupied: {self.part1}", out)
        self.assertEqual(snapshot(self.root), self.before, "something was overwritten")


class Benchmark(unittest.TestCase):
    """The speed test `apply` runs first writes and removes only files of its own."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="plexrenamer-bench-")
        os.makedirs(os.path.join(self.root, "Movies"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_it_leaves_the_volume_root_as_it_was(self):
        before = sorted(os.listdir(self.root))
        self.assertGreater(executor.benchmark(self.root, samples=5), 0)
        self.assertEqual(sorted(os.listdir(self.root)), before)

    def test_a_folder_of_its_old_name_is_not_touched(self):
        old = os.path.join(self.root, ".plexrenamer-benchmark")
        os.makedirs(old)
        names = ["bench-003", "bench-003.moved", "keep-me.txt"]
        for name in names:
            write(os.path.join(old, name), name)
        executor.benchmark(self.root, samples=5)
        self.assertEqual(sorted(os.listdir(old)), names)
        self.assertEqual([read(os.path.join(old, name)) for name in names], names)

    def test_a_link_of_its_old_name_does_not_lead_it_into_the_library(self):
        library = os.path.join(self.root, "Movies", "Some Folder")
        os.makedirs(library)
        write(os.path.join(library, "bench-001"), "a library file")
        os.symlink(library, os.path.join(self.root, ".plexrenamer-benchmark"))
        executor.benchmark(self.root, samples=5)
        self.assertEqual(os.listdir(library), ["bench-001"])
        self.assertEqual(read(os.path.join(library, "bench-001")), "a library file")

    def test_a_file_that_turns_up_in_its_folder_stays_and_keeps_the_folder(self):
        real_time, dropped = executor.time.time, []

        def drop_a_file():
            if not dropped:
                folder = next(entry.path for entry in os.scandir(self.root)
                              if entry.name.startswith(".plexrenamer-benchmark"))
                dropped.append(os.path.join(folder, "somebody else's.txt"))
                write(dropped[0], "not the tool's")
            return real_time()
        with mock.patch.object(executor.time, "time", drop_a_file):
            executor.benchmark(self.root, samples=5)
        self.assertEqual(read(dropped[0]), "not the tool's")
        self.assertEqual(os.listdir(os.path.dirname(dropped[0])), ["somebody else's.txt"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
