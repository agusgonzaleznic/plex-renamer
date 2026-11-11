"""A run that stops half-way, and folder metadata, must still undo exactly.

Every test cuts a run off the way Ctrl-C or a kill does, at each step in turn,
then undoes it and compares files and directories with the original tree."""

from __future__ import annotations

import hashlib
import os
import unittest
from unittest import mock

from test_roundtrip import CATS, LibraryCase, SwapMove, contents, dirs, snapshot

from plexrenamer import db, executor, undo


class Interrupt(BaseException):
    """Stands in for Ctrl-C or a kill. It is not an Exception, so nothing in the
    tool catches it, which is the point."""


def cut(module, step_name, stop_at, after):
    """Patch one step so its stop_at-th call is cut off, either before it touches
    the disk or right after it has."""
    real, calls = getattr(module, step_name), {"n": 0}

    def step(*args):
        calls["n"] += 1
        if calls["n"] == stop_at and not after:
            raise Interrupt()
        result = real(*args)
        if calls["n"] == stop_at and after:
            raise Interrupt()
        return result
    return mock.patch.object(module, step_name, step)


class Recovery(LibraryCase):
    # Finder metadata sitting in folders the plan is about to empty
    EXTRA = {
        "Movies/Lanternfall (2025) [1080p] [WEBRip] [x265] [YTS.MX]/.DS_Store": "s",
        "Movies/Lanternfall (2025) [1080p] [WEBRip] [x265] [YTS.MX]/Subs/.DS_Store": "t",
        "Movies/The Quillhaven Collection/.DS_Store": "u",
        "Movies/The Quillhaven Collection/Quillhaven (1999)/.DS_Store": "v",
        "Series/Moss.Atlas.2023.S01.1080p.H265-TBK/.DS_Store": "w",
    }

    def _swap_journal(self, with_inodes=False):
        base = os.path.join(self.root, "cycle")
        os.makedirs(base)
        for name in ("a.mkv", "b.mkv"):
            with open(os.path.join(base, name), "w") as fh:
                fh.write(name)
        a, b = os.path.join(base, "a.mkv"), os.path.join(base, "b.mkv")
        ops = executor.build_ops([SwapMove(a, b, with_inodes), SwapMove(b, a, with_inodes)],
                                 [base])
        if with_inodes:
            self.assertTrue(all(op.inode for op in ops),
                            "every leg of a swap must name the file it moves")
        conn = db.connect(self.dbpath)
        run_id = db.create_run(conn, self.root, ["cycle"], "cycle")
        db.insert_ops(conn, run_id, ops)
        return conn, run_id

    def _swap_journal_with_inodes(self):
        return self._swap_journal(with_inodes=True)

    def _fresh_tree(self):
        self.tearDown()
        self.setUp()

    def _every_cut(self, journal, during_undo=False):
        """Cut a run off at each step in turn, before and after the step touches
        the disk, then undo and yield where it was cut."""
        module, step_name = (undo, "_settle") if during_undo else (executor, "_do")
        for after in (False, True):
            stop_at = 0
            while True:
                stop_at += 1
                self._fresh_tree()
                conn, run_id = journal()
                finished = True
                try:
                    if during_undo:
                        executor.apply_ops(conn, run_id, quiet=True)
                    with cut(module, step_name, stop_at, after):
                        if during_undo:
                            undo.undo_run(conn, run_id, quiet=True)
                        else:
                            executor.apply_ops(conn, run_id, quiet=True)
                except Interrupt:
                    finished = False
                conn.close()                  # the process is gone
                if finished:
                    break                     # stop_at is past the last step
                conn = db.connect(self.dbpath)
                _ok, failed = undo.undo_run(conn, run_id, quiet=True)
                conn.close()
                where = f"cut {'after' if after else 'before'} step {stop_at}"
                self.assertEqual(failed, 0, where)
                yield where

    def test_apply_cut_off_anywhere_is_fully_undone(self):
        before, before_dirs = snapshot(self.root), dirs(self.root)
        cuts = 0
        for where in self._every_cut(self._journal):
            cuts += 1
            self.assertEqual(snapshot(self.root), before, where)
            self.assertEqual(dirs(self.root), before_dirs, where)
        self.assertGreater(cuts, 60, "the loop did not reach the interruption points")

    def test_undo_cut_off_anywhere_can_be_run_again(self):
        before, before_dirs = snapshot(self.root), dirs(self.root)
        cuts = 0
        for where in self._every_cut(self._journal, during_undo=True):
            cuts += 1
            self.assertEqual(snapshot(self.root), before, where)
            self.assertEqual(dirs(self.root), before_dirs, where)
        self.assertGreater(cuts, 60, "the loop did not reach the interruption points")

    def test_swap_cut_off_anywhere_is_fully_undone(self):
        cuts = 0
        for journal in (self._swap_journal, self._swap_journal_with_inodes):
            for during_undo in (False, True):
                for where in self._every_cut(journal, during_undo):
                    cuts += 1
                    base = os.path.join(self.root, "cycle")
                    self.assertEqual(sorted(os.listdir(base)), ["a.mkv", "b.mkv"], where)
                    for name in ("a.mkv", "b.mkv"):
                        with open(os.path.join(base, name)) as fh:
                            self.assertEqual(fh.read(), name, where)
        self.assertEqual(cuts, 24, "three steps, cut before and after, in apply and in "
                                   "undo, with and without inodes on record")

    def test_a_cut_between_two_operations_leaves_nothing_in_flight(self):
        conn, run_id = self._journal()
        real, starts = db.mark_op, {"n": 0}

        def mark(conn_, op_id, status, *args, **kwargs):
            if status == "started":
                starts["n"] += 1
                if starts["n"] == 10:
                    raise Interrupt()
            real(conn_, op_id, status, *args, **kwargs)
        with self.assertRaises(Interrupt), mock.patch.object(db, "mark_op", mark):
            executor.apply_ops(conn, run_id, quiet=True)
        conn.close()                          # the process is gone
        conn = db.connect(self.dbpath)
        states = dict(conn.execute(
            "SELECT status, COUNT(*) FROM op WHERE run_id=? GROUP BY status",
            (run_id,)).fetchall())
        self.assertNotIn("started", states, "a finished operation is not recorded as done")
        self.assertEqual(states.get("done"), 9)

    def test_every_journal_mark_lost_is_still_fully_undone(self):
        before, before_dirs = snapshot(self.root), dirs(self.root)
        conn, run_id = self._journal()
        _ok, failed = executor.apply_ops(conn, run_id, quiet=True)
        self.assertEqual(failed, 0)
        conn.execute("UPDATE op SET status='pending' WHERE run_id=?", (run_id,))
        conn.commit()                         # as if no mark ever reached the disk
        _ok, failed = undo.undo_run(conn, run_id, quiet=True)
        self.assertEqual(failed, 0)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(dirs(self.root), before_dirs)

    def test_folder_metadata_is_journalled_and_comes_back(self):
        before, before_dirs = snapshot(self.root), dirs(self.root)
        before_contents = contents(self.root)
        conn, run_id = self._journal()
        _ok, failed = executor.apply_ops(conn, run_id, quiet=True)
        self.assertEqual(failed, 0)
        self.assertEqual(set(contents(self.root)), set(before_contents),
                         "a file disappeared during apply")
        journalled = {row["src"] for row in conn.execute(
            "SELECT src FROM op WHERE run_id=? AND kind='rename' AND status='done'",
            (run_id,))}
        for rel in self.EXTRA:
            path = os.path.join(self.root, rel)
            self.assertFalse(os.path.lexists(path), f"still in its old folder: {rel}")
            self.assertIn(path, journalled, f"moved without a journal entry: {rel}")
        _good, bad, problems = executor.verify_run(conn, run_id)
        self.assertEqual(bad, 0, problems)
        _ok, failed = undo.undo_run(conn, run_id, quiet=True)
        self.assertEqual(failed, 0)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(dirs(self.root), before_dirs)

    def test_a_folder_removed_by_a_prune_run_comes_back(self):
        # a prune run holds nothing but rmdir rows, so no file move recreates the folder
        before, before_dirs = snapshot(self.root), dirs(self.root)
        shell = os.path.join(self.root, "Movies", "The Quillhaven Collection",
                             "Quillhaven (1999)", "Featurettes", "emptied earlier")
        bare = os.path.join(os.path.dirname(shell), "emptied too")
        os.makedirs(shell)
        os.makedirs(bare)                         # nothing in it, not even metadata
        with open(os.path.join(shell, ".DS_Store"), "w") as fh:
            fh.write("x" * 32)
        before_prune, dirs_before_prune = snapshot(self.root), dirs(self.root)
        conn = db.connect(self.dbpath)
        run_id = db.create_run(conn, self.root, CATS, "prune")
        db.insert_ops(conn, run_id, [
            executor.Op(n, "rmdir", path, None, True, 0, 0, "", "stale shell")
            for n, path in enumerate((shell, bare), 1)])
        self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (2, 0))
        self.assertFalse(os.path.lexists(shell) or os.path.lexists(bare))
        self.assertNotEqual(dirs(self.root), dirs_before_prune)
        self.assertEqual(undo.undo_run(conn, run_id, quiet=True)[1], 0)
        self.assertEqual(snapshot(self.root), before_prune)
        self.assertEqual(dirs(self.root), dirs_before_prune)
        self.assertNotEqual(before, before_prune)
        self.assertNotEqual(before_dirs, dirs_before_prune)

    def test_metadata_stays_put_when_its_folder_is_not_removed(self):
        conn, run_id = self._journal()
        scene = os.path.join(self.root, "Series", "Moss.Atlas.2023.S01.1080p.H265-TBK")
        with open(os.path.join(scene, "notes.txt"), "w") as fh:
            fh.write("arrived after the plan was made")
        _ok, failed = executor.apply_ops(conn, run_id, quiet=True)
        self.assertEqual(failed, 1, "the rmdir of the folder that is no longer empty")
        self.assertTrue(os.path.lexists(os.path.join(scene, ".DS_Store")),
                        "metadata was pulled out of a folder that stayed")
        self.assertTrue(os.path.lexists(os.path.join(scene, "notes.txt")))

    def test_undo_keeps_metadata_that_appeared_after_the_run(self):
        before, before_dirs = snapshot(self.root), dirs(self.root)
        conn, run_id = self._journal()
        _ok, failed = executor.apply_ops(conn, run_id, quiet=True)
        self.assertEqual(failed, 0)
        created = os.path.join(self.root, "Movies", "Lanternfall (2025)")
        self.assertTrue(os.path.isdir(created))
        late = {".DS_Store": "finder wrote this later", "Thumbs.db": "so did explorer"}
        for name, payload in late.items():
            with open(os.path.join(created, name), "w") as fh:
                fh.write(payload)
        _ok, failed = undo.undo_run(conn, run_id, quiet=True)
        self.assertEqual(failed, 0)
        after = snapshot(self.root)
        for rel, digest in before.items():
            self.assertEqual(after.get(rel), digest, f"not restored: {rel}")
        for payload in late.values():
            self.assertIn(hashlib.sha256(payload.encode()).hexdigest(), after.values(),
                          "undo deleted a file that appeared after the run")
        self.assertEqual(dirs(self.root), before_dirs,
                         "a directory the run created is still there")


if __name__ == "__main__":
    unittest.main(verbosity=2)
