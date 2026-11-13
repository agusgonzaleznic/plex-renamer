"""A share does not have to number its files the way a disk does.

Both of these were seen on one SMB share mounted on macOS:

  - 64-bit numbers, with about half of them above 2**63 - 1, the largest
    integer SQLite stores;
  - numbers worked out from the path (the folder's number plus a hash of the
    name), so that a file gets another number with every rename, keeps it over
    a remount, and shares it with whatever else is put at that path.

The library is put on such numbers here. On the second kind a number never
proves which file is which, so the round trip, recovery and safety suites all
run again on it."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import io
import os
import sqlite3
import unittest
from unittest import mock

import test_recovery
import test_roundtrip
import test_undo_command
import test_undo_safety
from test_roundtrip import CATS, LibraryCase, dirs, snapshot
from test_undo_safety import MOVIE, read, write

from plexrenamer import cli, db, executor, scan, undo

TOP = 1 << 63


def high(path, st):
    """The number the disk gives, moved into the upper half of 64 bits."""
    return st.st_ino | TOP


def by_path(path, st):
    """A number worked out from the path alone: the folder's plus a hash of the name."""
    parent, name = os.path.split(os.path.abspath(path))
    if not name:
        return 2
    digest = hashlib.blake2b(os.fsencode(name), digest_size=8).digest()
    return (by_path(parent, st) + int.from_bytes(digest, "big")) % (1 << 64) or 1


class _Entry:
    """A directory entry whose stat() reports the share's number."""

    def __init__(self, entry, renumber):
        self._entry, self._renumber = entry, renumber

    def __getattr__(self, name):
        return getattr(self._entry, name)

    def __fspath__(self):
        return self._entry.path

    def stat(self, *, follow_symlinks=True):
        return self._renumber(self._entry.path,
                              self._entry.stat(follow_symlinks=follow_symlinks))


class _Os:
    """The os module as the tool sees it on such a share: every inode number it
    reads is number(path, st), and everything else is the real thing."""

    def __init__(self, number):
        self._number = number

    def __getattr__(self, name):
        return getattr(os, name)

    def _renumber(self, path, st):
        return os.stat_result((st.st_mode, self._number(os.fspath(path), st)) + tuple(st)[2:10])

    def lstat(self, path, **kwargs):
        return self._renumber(path, os.lstat(path, **kwargs))

    def scandir(self, path="."):
        return [_Entry(entry, self._renumber) for entry in os.scandir(path)]


def share(number):
    """Put the tool on a share that numbers its files with number(path, st).

    Only the modules that read an inode number get the other os, so the test
    machinery around them (copying a tree, removing one) is left alone.
    """
    shown = _Os(number)
    stack = contextlib.ExitStack()
    for module in (scan, executor, test_roundtrip):
        stack.enter_context(mock.patch.object(module, "os", shown))
    return stack


def row(src, dst, **fields):
    """A journal row, as far as the executor reads one."""
    st = os.lstat(fields.pop("at", src))
    made = {"kind": "rename", "src": src, "dst": dst, "is_dir": 0, "size": st.st_size,
            "inode": db.inode_of(st), "status": "done"}
    made.update(fields)
    return made


class InodeForm(unittest.TestCase):
    def test_every_64_bit_number_fits_the_journal_and_stays_distinct(self):
        numbers = [1, 5444, TOP - 1, TOP, TOP + 1, 18440036704401854091, 2 * TOP - 1]
        forms = [db.inode_of(os.stat_result((0, n) + (0,) * 8)) for n in numbers]
        self.assertEqual(len(set(forms)), len(numbers))
        self.assertEqual(forms[:3], numbers[:3], "a number that already fits must not change")
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t(i INTEGER)")
        conn.executemany("INSERT INTO t VALUES (?)", [(f,) for f in forms])
        self.assertEqual([r[0] for r in conn.execute("SELECT i FROM t ORDER BY rowid")], forms)
        self.assertTrue(all(forms), "0 means that no inode was recorded")

    def test_the_raw_number_is_what_sqlite_refuses(self):
        # the control: without inode_of, the largest numbers cannot be stored at all
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t(i INTEGER)")
        with self.assertRaises(OverflowError):
            conn.execute("INSERT INTO t VALUES (?)", (TOP,))


class HighNumbers(LibraryCase):
    EXTRA = test_recovery.Recovery.EXTRA      # folder metadata, so it is lifted too

    def test_the_scan_reads_every_number_in_the_journal_form(self):
        with share(high):
            trees = scan.scan(self.root, CATS)
        nodes = [node for tree in trees.values() for node in tree.walk()]
        self.assertGreater(len(nodes), 20)
        self.assertTrue(all(node.inode < 0 for node in nodes),
                        "a number from the upper half reached the plan as it was")

    def test_a_whole_run_is_applied_verified_and_undone(self):
        before, before_dirs = snapshot(self.root), dirs(self.root)
        with share(high):
            conn, run_id = self._journal()
            planned = [r["inode"] for r in db.run_ops(conn, run_id) if r["kind"] == "rename"]
            self.assertTrue(planned and all(i < 0 for i in planned),
                            "the fixture is not on high numbers, so this would prove nothing")
            _ok, failed = executor.apply_ops(conn, run_id, quiet=True)
            self.assertEqual(failed, 0)
            lifted = [r["inode"] for r in db.run_ops(conn, run_id)
                      if r["reason"] == executor.METADATA_MOVE]
            self.assertTrue(lifted and all(i < 0 for i in lifted))
            self.assertFalse(executor.inodes_are_stale(db.run_ops(conn, run_id)),
                             "the numbers hold, so they must still count as proof")
            self.assertEqual(executor.verify_run(conn, run_id)[1:], (0, []))
            _ok, failed = undo.undo_run(conn, run_id, quiet=True)
            self.assertEqual(failed, 0)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(dirs(self.root), before_dirs)

    def test_a_lost_reply_is_still_recognised(self):
        src = os.path.join(self.root, MOVIE)
        real = os.rename

        def lossy(old, new, *args, **kwargs):
            real(old, new, *args, **kwargs)
            if old == src:
                raise OSError(errno.EIO, "the reply was lost")
        with share(high):
            conn, run_id = self._journal()
            with mock.patch.object(os, "rename", lossy):
                _ok, failed = executor.apply_ops(conn, run_id, quiet=True)
        found = conn.execute("SELECT * FROM op WHERE run_id=? AND src=?", (run_id, src)).fetchone()
        self.assertEqual((failed, found["status"]), (0, "done"))
        self.assertIn("took effect", found["error"])

    def test_the_numbers_stay_in_force_as_proof_during_a_run(self):
        src = os.path.join(self.root, MOVIE)
        with share(high):
            conn, run_id = self._journal()
            os.rename(src, src + ".old")
            write(src, "s" * 32)                  # same size as the film, another file
            _ok, failed = executor.apply_ops(conn, run_id, quiet=True)
        self.assertEqual(failed, 1, "a rename keeps the number here, so it must be asked")
        self.assertEqual(read(src), "s" * 32)

    def test_the_commands_work_on_such_a_share(self):
        before, before_dirs = snapshot(self.root), dirs(self.root)
        base = ["--root", self.root, "--db", self.dbpath, "--categories", *CATS,
                "--decisions", os.path.join(self.state, "none")]
        with share(high):
            for command in (["apply", "--rate", "5000"], ["verify"], ["undo"]):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = cli.main(base + command)
                self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(dirs(self.root), before_dirs)


class WhichFilesAreAsked(LibraryCase):
    """Whether the journal's numbers still tell files apart is read off the files
    a run moved, since a number worked out from the path is always the recorded
    one for a file that never left."""

    def setUp(self):
        super().setUp()
        self.base = os.path.join(self.root, "Movies")
        self.paths = []
        for n in range(6):
            self.paths.append(os.path.join(self.base, f"film {n}.mkv"))
            write(self.paths[-1], f"film {n}")

    def _moved(self, n, **fields):
        """A row for film n, which is at its destination and so has a new inode
        number when the copy stands in for a rename on a renumbering share."""
        src, dst = self.paths[n], self.paths[n] + ".moved"
        made = row(src, dst, **fields)
        with open(src) as fh:
            write(dst, fh.read())
        os.remove(src)
        return made

    def test_a_moved_file_with_another_number_outweighs_the_ones_that_never_left(self):
        rows = [self._moved(0)] + [row(p, p + ".moved", status="pending")
                                   for p in self.paths[1:]]
        self.assertTrue(executor.inodes_are_stale(rows))
        self.assertFalse(executor.inodes_are_stale(rows[1:]),
                         "the control: without the moved file, every number is the recorded one")

    def test_a_moved_file_with_its_own_number_keeps_them_all_as_proof(self):
        src, dst = self.paths[0], self.paths[0] + ".moved"
        rows = [row(src, dst)] + [row(p, p + ".moved", status="pending")
                                  for p in self.paths[1:]]
        os.rename(src, dst)
        self.assertFalse(executor.inodes_are_stale(rows))

    def test_when_nothing_is_at_its_destination_the_sources_are_asked(self):
        rows = [row(p, p + ".moved", status="pending") for p in self.paths]
        self.assertFalse(executor.inodes_are_stale(rows))
        test_roundtrip.remount(self.root)
        self.assertTrue(executor.inodes_are_stale(rows), "every number is from another mount")

    def test_a_row_that_is_undone_has_no_say(self):
        kept = self.paths[0] + ".moved"
        rows = [row(self.paths[0], kept)]
        os.rename(self.paths[0], kept)
        for n in (1, 2):                      # undone long ago, another file is there now
            rows.append(self._moved(n, status="undone"))
        self.assertFalse(executor.inodes_are_stale(rows))
        for made in rows[1:]:
            made["status"] = "done"           # the control: standing rows are asked
        self.assertTrue(executor.inodes_are_stale(rows))

    def test_a_folder_where_a_file_was_put_is_not_that_file(self):
        rows = [row(p, p + ".moved", size=0) for p in self.paths[:3]]   # no size to go by
        os.rename(self.paths[0], self.paths[0] + ".moved")
        for p in self.paths[1:3]:
            os.mkdir(p + ".moved")            # a folder took the name; the files never left
        self.assertFalse(executor.inodes_are_stale(rows))

    def test_a_file_of_another_size_where_a_file_was_put_is_not_that_file(self):
        rows = [row(p, p + ".moved") for p in self.paths[:3]]
        os.rename(self.paths[0], self.paths[0] + ".moved")
        for p in self.paths[1:3]:
            write(p + ".moved", "somebody else's, and longer")    # the files never left
        self.assertFalse(executor.inodes_are_stale(rows))


class LostReply(LibraryCase):
    """What proves that a rename went through although the share said it failed."""

    def setUp(self):
        super().setUp()
        self.src = os.path.join(self.root, MOVIE)
        self.dst = self.src + ".moved"
        self.row = row(self.src, self.dst)
        os.rename(self.src, self.dst)

    def test_where_a_moved_file_keeps_its_number_the_number_proves_it(self):
        for keeps in (True, None):
            self.assertTrue(executor._took_effect(self.row, keeps))
            self.assertFalse(executor._took_effect(dict(self.row, inode=self.row["inode"] + 1),
                                                   keeps), "another file is at the destination")

    def test_where_it_gets_another_number_kind_and_size_prove_it(self):
        renumbered = dict(self.row, inode=self.row["inode"] + 1)
        self.assertTrue(executor._took_effect(renumbered, False))
        self.assertFalse(executor._took_effect(dict(renumbered, size=0), False),
                         "without a size, nothing tells the file from another")
        self.assertFalse(executor._took_effect(dict(renumbered, size=7), False))
        self.assertFalse(executor._took_effect(dict(renumbered, is_dir=1), False))
        self.assertFalse(executor._took_effect(dict(renumbered, inode=0), False),
                         "a row without a number on record proves nothing")
        write(self.src)
        self.assertFalse(executor._took_effect(renumbered, False), "the source is still there")

    def test_a_destination_that_cannot_be_read_settles_nothing(self):
        self.assertIs(executor._kept_number(self.row), True)
        self.assertIs(executor._kept_number(dict(self.row, inode=self.row["inode"] + 1)), False)
        self.assertIsNone(executor._kept_number(dict(self.row, dst=self.dst + ".gone")))


class _PathNumbers:
    """Runs a suite on a share whose inode numbers are worked out from the path."""

    def setUp(self):
        if getattr(self, "_shown", None) is None:     # a test may call setUp again
            self._shown = share(by_path)
            self._shown.__enter__()
            self.addCleanup(self._shown.close)
        super().setUp()


class RoundTripOnPathNumbers(_PathNumbers, test_roundtrip.RoundTrip):
    def test_the_numbers_here_do_follow_the_path(self):
        # the control: on this share a rename does change the number
        conn, run_id = self._journal()
        first = next(r for r in db.run_ops(conn, run_id) if r["kind"] == "rename")
        self.assertTrue(executor.holds(first["src"], first["inode"]))
        self.assertEqual(executor.apply_ops(conn, run_id, quiet=True)[1], 0)
        self.assertFalse(executor.holds(first["dst"], first["inode"]))
        self.assertTrue(executor.inodes_are_stale(db.run_ops(conn, run_id)))
        self.assertEqual(executor.verify_run(conn, run_id)[1:], (0, []))


class RecoveryOnPathNumbers(_PathNumbers, test_recovery.Recovery):
    def test_a_swap_goes_through_with_inodes_on_record(self):
        conn, run_id = self._swap_journal_with_inodes()
        self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (3, 0),
                         "the second leg starts from a temporary name, which has another number")
        base = os.path.join(self.root, "cycle")
        self.assertEqual((read(os.path.join(base, "a.mkv")), read(os.path.join(base, "b.mkv"))),
                         ("b.mkv", "a.mkv"))


class UndoCommandOnPathNumbers(_PathNumbers, test_undo_command.UndoCommand):
    pass


class MetadataNamesOnPathNumbers(_PathNumbers, test_undo_safety.MetadataNames):
    pass


class UndoSafetyOnPathNumbers(_PathNumbers, test_undo_safety.UndoSafety):
    # Two promises rest on an inode that holds, and here none does. Each is
    # replaced by a test that states what is left of it.
    test_on_one_mount_a_stranger_of_the_same_size_is_not_taken_back = None
    test_apply_refuses_a_source_of_the_same_size_that_is_another_file = None

    def test_a_stranger_of_the_same_name_kind_and_size_cannot_be_told_apart(self):
        conn, run_id = self._cut_at_the_movie(after=False)
        os.rename(self.src, os.path.join(self.root, "Movies", "moved by hand.mkv"))
        write(self.dst, "s" * 32)                 # same size as the film, another file
        undo.undo_run(conn, run_id, quiet=True)
        self.assertEqual(read(self.src), "s" * 32, "name, kind and size are all there is here")
        self.assertFalse(os.path.lexists(self.dst))

    def test_a_source_replaced_by_one_of_the_same_size_is_moved_as_planned(self):
        conn, run_id = self._journal()
        os.rename(self.src, self.src + ".old")
        write(self.src, "s" * 32)
        self.assertEqual(executor.apply_ops(conn, run_id, quiet=True)[1], 0)
        self.assertEqual(read(self.dst), "s" * 32, "name, kind and size are all there is here")

    def test_the_first_reply_lost_is_on_record_as_failed_and_still_undone(self):
        # until one rename has shown what this share does to a number, a lost
        # reply proves nothing; undo finds the file by its size all the same
        conn, run_id = self._journal()
        first = next(r for r in db.run_ops(conn, run_id) if r["kind"] == "rename")
        real = os.rename

        def lossy(src, dst, *args, **kwargs):
            real(src, dst, *args, **kwargs)
            if src == first["src"]:
                raise OSError(errno.EIO, "the reply was lost")
        with mock.patch.object(os, "rename", lossy):
            _ok, failed = executor.apply_ops(conn, run_id, quiet=True)
        self.assertEqual(failed, 1)
        self.assertTrue(os.path.lexists(first["dst"]), "the rename did go through")
        self._assert_restored(conn, run_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
