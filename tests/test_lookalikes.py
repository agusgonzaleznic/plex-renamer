"""Things undo cannot tell apart: two files of one size, two folders, two empty files.

Where a run was to move such look-alikes along one path (a swap, or a to b and
b to c), name, kind and size say the same about both. On a disk the inode still
tells them apart. On a share that works its numbers out from the path nothing
does, and the journal has to decide what the disk cannot. The suite runs on
both.

Every test follows a thing by the number the disk itself gives it. The tool
does not get to see that number on such a share."""

from __future__ import annotations

import contextlib
import errno
import io
import os
import stat
import unittest
from unittest import mock

import test_share_numbers
import test_undo_command
from test_recovery import Interrupt, cut
from test_review_gaps import hiccup
from test_roundtrip import CATS, LibraryCase
from test_undo_safety import write

from plexrenamer import cli, db, executor, undo

SHAPES = {"swap": (("a", "b"), ("b", "a")), "chain": (("a", "b"), ("b", "c"))}
KINDS = ("sized", "empty", "folder")
# Two ordinary renames ahead of the look-alikes. Their files show what the
# volume does to an inode number, as the rest of a real run would.
LEADS = (("lead", "lead (2000)"), ("other", "other (2001)"))


class Move:
    """A planned move of what is on disk, with the inode number the tool reads there."""

    def __init__(self, src, dst):
        st = executor.os.lstat(src)               # on a share, the share's number
        self.src, self.dst = src, dst
        self.is_dir = stat.S_ISDIR(st.st_mode)
        self.size = 0 if self.is_dir else st.st_size
        self.inode = db.inode_of(st)
        self.category, self.unit, self.reason = "Movies", "", "look-alike"


@contextlib.contextmanager
def refusing(src):
    """The share refuses to rename src, for a real reason, and nothing moves."""
    real = os.rename

    def rename(a, b, *args, **kwargs):
        if a == src:
            raise PermissionError(errno.EACCES, "permission denied", a)
        return real(a, b, *args, **kwargs)
    with mock.patch.object(os, "rename", rename):
        yield


@contextlib.contextmanager
def losing_the_reply(src=None):
    """The share renames src, or anything, and the reply is lost on the way back."""
    real = os.rename

    def rename(a, b, *args, **kwargs):
        real(a, b, *args, **kwargs)
        if src in (None, a):
            raise OSError(errno.EIO, "the reply was lost")
    with mock.patch.object(os, "rename", rename):
        yield


def between_rows(stop_at):
    """Cut a run off between two rows: right before the stop_at-th one is marked."""
    real, starts = db.mark_op, {"n": 0}

    def mark(conn, op_id, status, *args, **kwargs):
        if status == "started":
            starts["n"] += 1
            if starts["n"] == stop_at:
                raise Interrupt()
        return real(conn, op_id, status, *args, **kwargs)
    return mock.patch.object(db, "mark_op", mark)


class LookAlikeCase(LibraryCase):
    """A folder of look-alikes next to two ordinary files, and runs over it."""

    _cli = test_undo_command.UndoCommand._cli
    _run_status = test_undo_command.UndoCommand._run_status

    def setUp(self):
        super().setUp()
        self.root_as_typed = self.root
        self.base = os.path.join(self.root, "Movies", "alike")

    def _fresh(self):
        self.tearDown()
        self.setUp()

    def _path(self, name):
        return os.path.join(self.base, name)

    def _make(self, kind, sizes=(4, 4), names="ab"):
        """Look-alikes, a and b by default, next to two ordinary files. Returns where each is."""
        os.makedirs(self.base)
        for name, size in zip(names, sizes):
            self._put(kind, name, name * size)
        write(self._path("lead"), "an ordinary file")
        write(self._path("other"), "another ordinary file")
        return self._where()

    def _put(self, kind, name, text):
        if kind == "folder":
            os.makedirs(self._path(name))
        else:
            write(self._path(name), "" if kind == "empty" else text)

    def _where(self):
        """Which thing is under which name, by the number the disk gives it."""
        return {name: os.lstat(self._path(name)).st_ino
                for name in sorted(os.listdir(self.base))}

    def _all(self):
        """The same for everything under the folder, at any depth."""
        found = {}
        for top, dirs, files in os.walk(self.base):
            for name in dirs + files:
                path = os.path.join(top, name)
                found[os.path.relpath(path, self.base)] = os.lstat(path).st_ino
        return found

    def _runs(self, *runs):
        """Apply each (pairs, patch) in turn, the way the command does."""
        for pairs, patch in runs:
            conn, run_id = self._journal_of(pairs)
            with patch or contextlib.nullcontext(), contextlib.redirect_stdout(io.StringIO()):
                cli.run_ops(conn, run_id, "look-alikes")
            conn.close()

    def _notes(self, run_id):
        conn = db.connect(self.dbpath)
        try:
            return {(r["src"], r["dst"]): (r["status"], r["error"] or "")
                    for r in db.run_ops(conn, run_id)}
        finally:
            conn.close()

    def _journal_of(self, pairs, leads=False):
        pairs = (LEADS if leads else ()) + tuple(pairs)
        moves = [Move(self._path(src), self._path(dst)) for src, dst in pairs]
        conn = db.connect(self.dbpath)
        run_id = db.create_run(conn, self.root, CATS, "look-alikes")
        db.insert_ops(conn, run_id, executor.build_ops(moves, self._boundaries()))
        return conn, run_id

    def _stops(self, renames):
        """Every way a run of so many renames can stop, as (where, a patch or None)."""
        yield "not cut", None
        for n in range(1, renames + 1):
            yield f"cut before rename {n}", cut(executor, "_do", n, after=False)
            yield f"cut right after rename {n}", cut(executor, "_do", n, after=True)
            yield f"cut before row {n} is marked", between_rows(n)

    def _failed_swap(self, kind, sizes=(4, 4), older=False):
        """A swap whose first leg the share refuses, so that no leg goes through.

        With older, the journal reads like one written before a rename refused
        before it was tried was told apart from one that was tried.
        """
        before = self._make(kind, sizes)
        conn, run_id = self._journal_of(SHAPES["swap"], leads=True)
        with refusing(self._path("a")):
            self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (2, 3), kind)
        # The first leg was tried. The two after it found a name taken and a
        # source gone, so they were refused before the rename: they are not
        # asked whether they went through after all, and say they were not tried.
        rows = db.run_ops(conn, run_id)
        self.assertEqual([r["status"] for r in rows],
                         ["done", "done", "failed", "failed", "failed"], kind)
        self.assertEqual([r["error"].startswith(executor.NOT_TRIED) for r in rows[2:]],
                         [False, True, True], kind)
        if older:
            conn.execute("UPDATE op SET error = substr(error, ?) WHERE run_id = ? "
                         "AND error LIKE ?", (len(executor.NOT_TRIED) + 1, run_id,
                                              executor.NOT_TRIED + "%"))
            conn.commit()
            self.assertFalse([r for r in db.run_ops(conn, run_id)
                              if (r["error"] or "").startswith(executor.NOT_TRIED)])
        return before, conn, run_id


class LookAlikes(LookAlikeCase):
    """A swap and a chain of look-alikes, on the numbers a disk gives."""

    # The kinds a lost reply is proven for after all: on a disk, the inode
    # proves it for every kind.
    PROVEN = KINDS

    # -- a run that was cut off ------------------------------------------------

    def test_a_swap_or_a_chain_cut_off_anywhere_is_fully_undone(self):
        cases = moved = 0
        for kind in KINDS:
            for shape, pairs in SHAPES.items():
                for where, stop in self._stops(len(pairs) + (shape == "swap")):
                    self._fresh()
                    case = f"{kind} {shape}, {where}"
                    before = self._make(kind)
                    conn, run_id = self._journal_of(pairs)
                    with contextlib.suppress(Interrupt), stop or contextlib.nullcontext():
                        executor.apply_ops(conn, run_id, quiet=True)
                    conn.close()                  # the process is gone
                    moved += self._where() != before
                    conn = db.connect(self.dbpath)
                    self.assertEqual(undo.undo_run(conn, run_id, quiet=True)[1], 0, case)
                    self.assertEqual(self._where(), before, case)
                    # Looked at again, a row that never ran must not take what
                    # undo has just put back for the file it was to move.
                    self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (0, 0), case)
                    self.assertEqual(self._where(), before, case)
                    conn.close()
                    cases += 1
        self.assertEqual(cases, 3 * (10 + 7), "three kinds, a swap and a chain, every cut")
        self.assertEqual(moved, 3 * (8 + 5), "the control: most cuts leave things moved")

    def test_the_command_calls_a_cut_off_swap_undone_and_does_not_look_again(self):
        before = self._make("sized")
        conn, run_id = self._journal_of(SHAPES["swap"])
        with cut(executor, "_do", 2, after=True), self.assertRaises(Interrupt), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.run_ops(conn, run_id, "swap")
        conn.close()                              # the process is gone
        self.assertNotEqual(self._where(), before)
        code, out = self._cli("undo")
        self.assertEqual(code, 0, out)
        self.assertIn(f"undoing run {run_id}", out)
        self.assertEqual((self._run_status(run_id), self._where()), ("undone", before))
        code, out = self._cli("undo", "--run", str(run_id))
        self.assertEqual(code, 0, out)
        self.assertIn(f"run {run_id} is already undone", out)
        self.assertNotIn("undoing", out, "an undone run was looked at again")
        self.assertEqual(self._where(), before)
        self.assertIn("nothing to undo", self._cli("undo")[1])

    def test_a_move_cut_off_before_it_ran_is_not_blamed_for_what_took_its_new_name(self):
        # Behind the last mark the move never ran. What has taken its new name
        # since is not what it moved, also where nothing tells the two apart.
        for kind in KINDS:
            self._fresh()
            before = self._make(kind)
            conn, run_id = self._journal_of([("a", "moved")], leads=True)
            with between_rows(3), contextlib.suppress(Interrupt):
                executor.apply_ops(conn, run_id, quiet=True)
            conn.close()                          # the process is gone
            self._put(kind, "moved", "nnnnn")
            newcomer = os.lstat(self._path("moved")).st_ino
            conn = db.connect(self.dbpath)
            for reversed_ in (2, 0):
                self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (reversed_, 0), kind)
                self.assertEqual(self._where(), {**before, "moved": newcomer}, kind)
            conn.close()

    # -- a leg that failed, or that cannot be read -----------------------------

    def test_a_swap_whose_first_leg_failed_is_settled(self):
        # The legs refused before the rename never touched the disk, and the
        # first one is found where it started. That holds on any share.
        for kind in KINDS:
            self._fresh()
            before, conn, run_id = self._failed_swap(kind)
            for reversed_ in (2, 0):
                self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (reversed_, 0), kind)
                self.assertEqual(self._where(), before, kind)

    def test_from_an_older_journal_a_failed_swap_is_settled_where_inodes_tell_files_apart(self):
        for kind in KINDS:
            self._fresh()
            before, conn, run_id = self._failed_swap(kind, older=True)
            self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (2, 0), kind)
            self.assertEqual(self._where(), before, kind)
            # A second look has only the failed legs left to judge the numbers
            # by. Whatever it then reports, it moves nothing.
            undo.undo_run(conn, run_id, quiet=True)
            self.assertEqual(self._where(), before, kind)

    def test_a_swap_cut_off_after_its_first_leg_failed_moves_nothing(self):
        # The last leg is on record as started, so it is expected to have run,
        # yet nothing ever reached the temporary name it moves from. What is at
        # its destination is the look-alike that the leg before it, refused
        # before the rename, never moved away.
        for kind in KINDS:
            self._fresh()
            before = self._make(kind)
            conn, run_id = self._journal_of(SHAPES["swap"], leads=True)
            with refusing(self._path("a")), cut(executor, "_do", 5, after=False), \
                    contextlib.suppress(Interrupt):
                executor.apply_ops(conn, run_id, quiet=True)
            conn.close()                          # the process is gone
            conn = db.connect(self.dbpath)
            self.assertEqual([r["status"] for r in db.run_ops(conn, run_id)],
                             ["done", "done", "failed", "failed", "started"], kind)
            for reversed_ in (2, 0):
                self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (reversed_, 2), kind)
                self.assertEqual(self._where(), before, f"{kind}: what never moved was moved")
            conn.close()

    def test_a_rename_refused_before_it_was_tried_is_left_as_it_was(self):
        # The move of a to b found b taken. Once a is moved away by hand, b holds
        # a look-alike of a where the row was to put a, and undo must not take
        # it for a.
        before = self._make("sized")
        conn, run_id = self._journal_of([("a", "b")], leads=True)
        self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (2, 1))
        self.assertIn("destination occupied", db.run_ops(conn, run_id)[-1]["error"])
        os.rename(self._path("a"), self._path("c"))
        expected = {**before, "c": before["a"]}
        del expected["a"]
        for reversed_ in (2, 0):
            self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (reversed_, 0))
            self.assertEqual(self._where(), expected, "what was at b was taken for a")

    def test_a_leg_that_cannot_be_read_holds_back_the_older_legs_on_its_paths(self):
        before = self._make("sized")
        conn, run_id = self._journal_of(SHAPES["swap"])
        self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (3, 0))
        swapped = self._where()
        self.assertNotEqual(swapped, before)
        with hiccup(self._path("b"), undo, "_settle", lambda row, _state: row["seq"] == 3) as seen:
            self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (0, 3))
        self.assertEqual(seen["hits"], 1)
        # Settled in this pass, the two older legs would take what the last leg
        # left on their paths for their own files.
        self.assertEqual(self._where(), swapped, "an older leg was settled over a newer one")
        rows = db.run_ops(conn, run_id)
        self.assertEqual([r["status"] for r in rows], ["done"] * 3)
        self.assertIn("hiccuped", rows[2]["error"])
        for row in rows[:2]:
            self.assertIn("waits for a newer row", row["error"])
        self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (3, 0))
        self.assertEqual(self._where(), before)

    def test_a_file_taken_back_is_not_mistaken_for_one_a_failed_row_moved(self):
        for kind in KINDS:
            self._fresh()
            before = self._make(kind)
            conn, run_id = self._journal_of(SHAPES["chain"])
            with refusing(self._path("a")):       # the second leg, a to b
                self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (1, 1), kind)
            self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (1, 0), kind)
            self.assertEqual(self._where(), before, kind)
            # b now holds the thing undo took back there. To the row that failed
            # it looks like the one it was to move, at its destination.
            self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (0, 0), kind)
            self.assertEqual(self._where(), before, kind)

    # -- a reply that was lost -------------------------------------------------

    def test_a_lost_reply_is_reversed_where_it_is_proven_and_reported_where_it_is_not(self):
        for kind in KINDS:
            self._fresh()
            before = self._make(kind)
            conn, run_id = self._journal_of([("a", "moved")], leads=True)
            with losing_the_reply(self._path("a")):
                _ok, failed = executor.apply_ops(conn, run_id, quiet=True)
            self.assertEqual(self._where()["moved"], before["a"], "the rename did go through")
            lost = db.run_ops(conn, run_id)[-1]
            if kind in self.PROVEN:
                self.assertEqual((failed, lost["status"]), (0, "done"), kind)
                self.assertIn("took effect although it reported", lost["error"])
                self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (3, 0), kind)
                self.assertEqual(self._where(), before, kind)
                continue
            # Neither a number nor a size says that the thing at the new name is
            # the one this row was to move. It is reported and stays, and the run
            # is never called undone while it does.
            self.assertEqual((failed, lost["status"]), (1, "failed"), kind)
            for reversed_ in (2, 0):
                self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (reversed_, 1), kind)
                self.assertEqual(self._where()["moved"], before["a"], kind)
                self.assertIn("cannot tell whether this run made this move",
                              db.run_ops(conn, run_id)[-1]["error"])
            os.rename(self._path("moved"), self._path("a"))       # put back by hand
            self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (0, 0), kind)
            self.assertEqual(self._where(), before, kind)

    def test_a_lost_reply_whose_old_name_is_taken_again_is_reported_and_nothing_moves(self):
        # Then a look-alike takes the old name. Proven or not, the move cannot
        # be taken back without the newcomer's place, and the run is never
        # called undone while it stays.
        for kind in KINDS:
            self._fresh()
            self._make(kind)
            conn, run_id = self._journal_of([("a", "moved")], leads=True)
            with losing_the_reply(self._path("a")):
                executor.apply_ops(conn, run_id, quiet=True)
            self._put(kind, "a", "nnnn")
            left = self._where()
            for reversed_ in (2, 0):
                self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (reversed_, 1), kind)
                now = self._where()
                self.assertEqual((now["a"], now["moved"]), (left["a"], left["moved"]), kind)
                self.assertIn("original path reoccupied" if kind in self.PROVEN else
                              "cannot tell whether this run made this move",
                              db.run_ops(conn, run_id)[-1]["error"], kind)

    # -- a journal that lost its marks -----------------------------------------

    def test_a_swap_whose_marks_were_all_lost_is_still_undone(self):
        for kind in KINDS:
            self._fresh()
            before = self._make(kind)
            conn, run_id = self._journal_of(SHAPES["swap"])
            self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (3, 0), kind)
            conn.execute("UPDATE op SET status='pending' WHERE run_id=?", (run_id,))
            conn.commit()                         # as if no mark ever reached the disk
            self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (3, 0), kind)
            self.assertEqual(self._where(), before, kind)


class LookAlikesOnPathNumbers(test_share_numbers._PathNumbers, LookAlikes):
    """The same on a share whose inode numbers follow the path.

    Some promises rest on an inode that holds, and here none does. Each is
    replaced by a test that states what is left of it.
    """

    PROVEN = ("sized",)                           # only a size proves a lost reply here
    test_from_an_older_journal_a_failed_swap_is_settled_where_inodes_tell_files_apart = None
    test_a_swap_whose_marks_were_all_lost_is_still_undone = None

    def test_the_numbers_here_do_follow_the_path(self):
        # the control: after a swap each name shows the number it showed before
        before = self._make("sized")
        seen = {name: executor.os.lstat(self._path(name)).st_ino for name in "ab"}
        conn, run_id = self._journal_of(SHAPES["swap"])
        self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (3, 0))
        self.assertEqual((self._where()["a"], self._where()["b"]), (before["b"], before["a"]))
        self.assertEqual({name: executor.os.lstat(self._path(name)).st_ino for name in "ab"},
                         seen)
        self.assertTrue(executor.inodes_are_stale(db.run_ops(conn, run_id)))

    def test_from_an_older_journal_a_failed_swap_is_reported_and_nothing_moves(self):
        for kind in KINDS:
            self._fresh()
            before, conn, run_id = self._failed_swap(kind, older=True)
            # Both names are taken and the temporary one is free. That is how
            # the swap looks before it starts and after it is through, and this
            # journal does not say which legs were never tried.
            for reversed_ in (2, 0):
                self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (reversed_, 3), kind)
                self.assertEqual(self._where(), before, f"{kind}: what never moved was moved")
            notes = [r["error"] for r in db.run_ops(conn, run_id)][2:]
            self.assertIn("cannot tell what is at", notes[2])
            for note in notes[:2]:
                self.assertIn("waits for a newer row", note)

    def test_with_sizes_that_differ_the_same_older_journal_is_settled(self):
        # the control: the doubt above is about look-alikes, not about a failed leg
        before, conn, run_id = self._failed_swap("sized", sizes=(4, 5), older=True)
        self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (2, 0))
        self.assertEqual(self._where(), before)
        self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (0, 0))

    def test_a_swap_whose_marks_were_all_lost_looks_like_one_that_never_ran(self):
        # The limit of deciding by the journal: with every mark gone, nothing on
        # this share tells a swap that went through from one that never started.
        for kind in KINDS:
            self._fresh()
            self._make(kind)
            conn, run_id = self._journal_of(SHAPES["swap"])
            self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (3, 0), kind)
            swapped = self._where()
            conn.execute("UPDATE op SET status='pending' WHERE run_id=?", (run_id,))
            conn.commit()
            self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (0, 0), kind)
            self.assertEqual(self._where(), swapped, kind)


if __name__ == "__main__":
    unittest.main(verbosity=2)
