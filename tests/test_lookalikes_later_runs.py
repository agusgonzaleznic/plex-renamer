"""An older run undone with --force while a later run on its paths still stands.

The later run may have moved look-alikes along the same paths, and what it left
there can pass for what the older run put there. Undo does not settle a row of
the older run on such a path until the later run is undone. The suite runs on
a disk and on a share whose inode numbers follow the path."""

from __future__ import annotations

import os
import unittest

import test_share_numbers
from test_lookalikes import KINDS, SHAPES, LookAlikeCase, losing_the_reply
from test_undo_safety import write


class ForcedUndoOfLookAlikes(LookAlikeCase):
    """Look-alikes on the numbers a disk gives."""

    # The kinds of look-alikes a run cannot be fully undone for, once every
    # reply of their swap was lost: on a disk, none.
    STUCK = ()

    def test_a_forced_undo_waits_for_a_later_run_that_moved_look_alikes_on_its_paths(self):
        # Run 1 swaps a and b. Run 2 swaps b and c, and the share loses every
        # reply. Where undo cannot settle run 2, what that run left at b passes
        # for what run 1 put there, so run 1, forced, waits for it.
        stuck = []
        for kind in KINDS:
            self._fresh()
            before = self._make(kind, (4, 4, 4), "abc")
            self._runs((SHAPES["swap"], None), ((("b", "c"), ("c", "b")), losing_the_reply()))
            self.assertIn("undoing run 2", self._cli("undo")[1], kind)
            left = self._where()
            code, out = self._cli("undo", "--run", "1", "--force")
            if self._run_status(2) == "undone":
                self.assertEqual((code, self._run_status(1), self._where()),
                                 (0, "undone", before), f"{kind}: {out}")
                continue
            stuck.append(kind)
            self.assertEqual((code, self._where()), (1, left), f"{kind}: {out}")
            notes = self._notes(1).values()
            self.assertEqual({status for status, _ in notes}, {"done"}, kind)
            # the two rows on b wait for run 2, the oldest one for them
            self.assertEqual(sum("waits for a later run" in note for _, note in notes), 2, kind)
        self.assertEqual(tuple(stuck), self.STUCK)

    def test_a_forced_undo_waits_for_a_later_run_that_swapped_the_folder_it_moved_into(self):
        # Run 1 moves x into folder a. Run 2 swaps a with b, a look-alike folder
        # that holds a look-alike of x, and the share loses every reply.
        os.makedirs(self._path("a"))
        os.makedirs(self._path("b"))
        write(self._path("x"), "xxxx")
        write(self._path("b/x"), "yyyy")
        before = self._all()
        self._runs(([("x", "a/x")], None), (SHAPES["swap"], losing_the_reply()))
        self.assertIn("undoing run 2", self._cli("undo")[1])
        left = self._all()
        code, out = self._cli("undo", "--run", "1", "--force")
        stuck = self._run_status(2) != "undone"
        self.assertEqual(stuck, "folder" in self.STUCK, out)
        if not stuck:
            self.assertEqual((code, self._all()), (0, before), out)
            return
        self.assertEqual((code, self._all()), (1, left), out)
        status, note = self._notes(1)[(self._path("x"), self._path("a/x"))]
        self.assertEqual(status, "done")
        self.assertIn("waits for a later run that still stands on", note)

    def test_a_forced_undo_waits_only_where_a_later_run_still_stands(self):
        # Run 1 renames folder a to b and file p to q. Run 2 renames x, inside b,
        # to y, and q to r. Then x's name is taken again, so undo reverses run 2's
        # move of q but not that of x. Run 1, forced, takes q back and leaves the
        # folder: moved now, it would carry run 2's file out of that run's reach.
        os.makedirs(self._path("a"))
        write(self._path("a/x"), "xxxx")
        write(self._path("p"), "pppp")
        before = self._all()
        self._runs(([("a", "b"), ("p", "q")], None), ([("b/x", "b/y"), ("q", "r")], None))
        write(self._path("b/x"), "a newcomer")
        self.assertIn("undoing run 2", self._cli("undo")[1])
        self.assertEqual(self._run_status(2), "partially-undone")
        code, out = self._cli("undo", "--run", "1", "--force")
        self.assertEqual(code, 1, out)
        notes = self._notes(1)
        self.assertEqual(notes[(self._path("p"), self._path("q"))][0], "undone")
        status, note = notes[(self._path("a"), self._path("b"))]
        self.assertEqual(status, "done")
        self.assertIn(f"still stands on {self._path('b')}", note)
        # With the newcomer out of the way, both runs are undone in their order.
        os.rename(self._path("b/x"), self._path("newcomer"))
        newcomer = os.lstat(self._path("newcomer")).st_ino
        for run_id in (2, 1):
            self.assertIn(f"undoing run {run_id}", self._cli("undo")[1])
        self.assertEqual((self._run_status(1), self._run_status(2)), ("undone", "undone"))
        self.assertEqual(self._all(), {**before, "newcomer": newcomer})


class ForcedUndoOfLookAlikesOnPathNumbers(test_share_numbers._PathNumbers,
                                          ForcedUndoOfLookAlikes):
    """The same on a share whose inode numbers follow the path."""

    STUCK = KINDS                                 # all of one size, nothing tells them apart


if __name__ == "__main__":
    unittest.main(verbosity=2)
