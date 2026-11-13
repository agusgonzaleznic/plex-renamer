"""Deleting torrent-site ads and .DS_Store files: only those, nothing else, and
every deletion written back by undo, byte for byte, with its time and mode."""

from __future__ import annotations

import os
import stat
import unittest
from unittest import mock

import test_undo_command
from test_roundtrip import CATS, LibraryCase, dirs, remount, snapshot

from plexrenamer import cli, db, executor, junk, undo
from plexrenamer.ops import Op

FILM = "Movies/Lanternfall (2025) [1080p] [WEBRip] [x265] [YTS.MX]"
AD = f"{FILM}/YTS.MX - Official site.jpg"
STORE = f"{FILM}/.DS_Store"
LOOKALIKE = "Series/Wickerby/DS Store"      # in the fixture, without the format's magic
NOT_ONE = "named like a .DS_Store, but it is not one"
WHEN = 1_500_000_000_123_456_789           # a modification time, in nanoseconds


def put(path, data: bytes, mode=0o644, mtime_ns=WHEN):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    os.chmod(path, mode)
    os.utime(path, ns=(mtime_ns, mtime_ns))


def ds_store(tail=b"finder view settings"):
    return junk.DS_STORE_MAGIC + tail


class Names(unittest.TestCase):
    """Which names count: ads of torrent sites and .DS_Store files, nothing near them."""

    def test_ads(self):
        for name in ("YTS.MX - Official site.jpg", ".YTS.BZ - Official site.jpg", "www.YTS.AM.jpg",
                     "YIFYStatus.com.txt", ".YIFYStatus.com.txt", ".YTSProxies.com.txt",
                     ".YTSYifyUP... (TOR).txt", ".hidden-RARBG.txt", "RARBG_DO_NOT_MIRROR.exe",
                     "Torrent Downloaded From ExtraTorrent.cc.txt",
                     "Torrent-Downloaded-From-ExtraTorrent.cc.txt"):
            self.assertEqual(junk.kind(name), junk.AD, name)

    def test_what_is_only_named_after_a_release_is_no_ad(self):
        for name in ("Lanternfall.2025.1080p.WEBRip.x265-[YTS.MX].jpg",
                     "Lanternfall.2025.1080p.WEBRip.x265-[YTS.MX].nfo",
                     ".Lanternfall.2025.1080p.WEBRip.x265-[YTS.MX].jpg",
                     "YTS.Lanternfall.2025.1080p.jpg", "YTS.MX 720p sample.jpg",
                     "[YTS.MX] poster.jpg", "Downloaded from the archive.txt"):
            self.assertIsNone(junk.kind(name), name)

    def test_ds_stores(self):
        for name in (".DS_Store", ".DS_Store-1", ".DS_Store-1-1", "DS Store", ".DS Store",
                     ".hidden-DS_Store", "ds_store"):
            self.assertEqual(junk.kind(name), junk.DS_STORE, name)

    def test_names_near_them_do_not_count(self):
        for name in ("._.DS_Store", "._YTS.MX - Official site.jpg", "readme.txt", "Info.txt",
                     "www.example.org.txt", "Lanternfall.2025.1080p.WEBRip.x265-[YTS.MX].mp4",
                     "YTS.MX.srt", "poster.jpg", "Thumbs.db", "desktop.ini", ".DS_Store.mkv",
                     ".DS_Store-x", "folder.jpg"):
            self.assertIsNone(junk.kind(name), name)


class Find(LibraryCase):
    """What is looked at before anything is deleted."""

    def test_the_fixture_holds_one_ad_and_one_lookalike(self):
        found, left = junk.find(self.root, CATS)
        self.assertEqual([j.path for j in found], [os.path.join(self.root, AD)])
        self.assertEqual(left, [(os.path.join(self.root, LOOKALIKE), NOT_ONE)])

    def test_only_a_real_small_plain_file_outside_the_nas_folders_counts(self):
        movies = os.path.join(self.root, "Movies")
        real = os.path.join(self.root, STORE)
        put(real, ds_store())
        big = os.path.join(movies, "YTS big.jpg")
        put(big, b"x" * (junk.MAX_BYTES + 1))
        link = os.path.join(movies, "YTS link.jpg")
        os.symlink(os.path.join(self.root, AD), link)
        for nas in ("@Recycle", "#recycle", ".@__thumb"):
            put(os.path.join(movies, nas, "YTS.MX - Official site.jpg"), b"thumb")
            put(os.path.join(movies, nas, ".DS_Store"), ds_store())
        found, left = junk.find(self.root, CATS)
        self.assertEqual(sorted(j.path for j in found),
                         sorted([os.path.join(self.root, AD), real]))
        why = dict(left)
        self.assertEqual(len(why), 3, left)
        self.assertIn("too big to keep in the journal", why[big])
        self.assertEqual(why[link], "not a plain file")
        self.assertEqual(why[os.path.join(self.root, LOOKALIKE)], NOT_ONE)


class Clean(LibraryCase):
    """`clean`, and the delete phase of `apply`, through the command line."""

    _cli = test_undo_command.UndoCommand._cli

    def setUp(self):
        super().setUp()
        self.root_as_typed = self.root
        self.ad, self.store = os.path.join(self.root, AD), os.path.join(self.root, STORE)
        put(self.ad, b"d" * 32, mode=0o604)
        put(self.store, ds_store(), mode=0o640)
        self.before, self.before_dirs = snapshot(self.root), dirs(self.root)

    def _rows(self, run_id=1):
        conn = db.connect(self.dbpath)
        try:
            return db.run_ops(conn, run_id), db.kept_of_run(conn, run_id)
        finally:
            conn.close()

    def assert_back_as_before(self):
        self.assertEqual(snapshot(self.root), self.before)
        self.assertEqual(dirs(self.root), self.before_dirs)
        for path, mode in ((self.ad, 0o604), (self.store, 0o640)):
            st = os.lstat(path)
            self.assertEqual((stat.S_IMODE(st.st_mode), st.st_mtime_ns), (mode, WHEN), path)

    def test_a_dry_run_lists_and_changes_nothing(self):
        code, out = self._cli("clean", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("to delete: 1 .DS_Store file, 1 torrent-site ad", out)
        self.assertIn(f"left in place, {NOT_ONE}: {LOOKALIKE}", out)
        self.assertFalse(os.path.exists(self.dbpath), "no journal was started")
        self.assertEqual(snapshot(self.root), self.before)

    def test_clean_deletes_only_junk_keeps_its_bytes_and_undo_writes_it_back(self):
        code, out = self._cli("clean")
        self.assertEqual(code, 0, out)
        self.assertEqual(snapshot(self.root),
                         {k: v for k, v in self.before.items() if k not in (AD, STORE)})
        self.assertEqual(dirs(self.root), self.before_dirs)
        self.assertIn("verify: 2 confirmed on disk, 0 mismatched", out)
        rows, kept = self._rows()
        self.assertEqual([(r["kind"], r["status"]) for r in rows], [("delete", "done")] * 2)
        for r in rows:
            self.assertEqual(kept[r["id"]]["sha256"],
                             self.before[os.path.relpath(r["src"], self.root)])
        code, out = self._cli("undo", "--dry-run")
        self.assertIn(f"write back  {self.ad}", out)
        code, out = self._cli("undo")
        self.assertEqual(code, 0, out)
        self.assert_back_as_before()

    def test_apply_deletes_what_its_moves_hide_and_lift_and_undo_brings_all_back(self):
        code, out = self._cli("apply", "--rate", "5000")
        self.assertEqual(code, 0, out)
        found, left = junk.find(self.root, CATS)
        self.assertEqual(found, [])
        self.assertEqual([why for _, why in left], [NOT_ONE])
        self.assertIn(" 0 mismatched", out)
        rows, kept = self._rows()
        deleted = [r for r in rows if r["kind"] == "delete"]
        self.assertEqual([r["status"] for r in deleted], ["done"] * 2)
        self.assertTrue(all(r["id"] in kept for r in deleted))
        self.assertEqual(self._cli("verify", "--run", "1")[0], 0)
        code, out = self._cli("undo")
        self.assertEqual(code, 0, out)
        self.assert_back_as_before()

    def test_a_written_back_file_does_not_make_the_numbers_look_stale(self):
        self.assertEqual(self._cli("apply", "--rate", "5000")[0], 0)
        hidden = os.path.join(self.root, "Movies", "Lanternfall (2025)",
                              ".YTS.MX - Official site.jpg")
        real = os.rename

        def stuck(src, dst, *args, **kwargs):
            if src == hidden:
                raise PermissionError(13, "Permission denied", src)
            return real(src, dst, *args, **kwargs)

        with mock.patch.object(undo.os, "rename", stuck):
            code, out = self._cli("undo")
        self.assertEqual(code, 1, out)
        self.assertTrue(os.path.lexists(hidden), "written back, its move not reversed yet")
        conn = db.connect(self.dbpath)
        try:
            self.assertFalse(executor.inodes_are_stale(db.run_ops(conn, 1)))
        finally:
            conn.close()
        code, out = self._cli("undo")
        self.assertEqual(code, 0, out)
        self.assert_back_as_before()

    def test_a_later_clean_finds_nothing_and_starts_no_run(self):
        self.assertEqual(self._cli("apply", "--rate", "5000")[0], 0)
        code, out = self._cli("clean")
        self.assertEqual(code, 0, out)
        self.assertIn("to delete: none", out)
        conn = db.connect(self.dbpath)
        try:
            self.assertIsNone(db.get_run(conn, 2))
        finally:
            conn.close()

    def test_apply_with_nothing_to_rename_still_deletes(self):
        self.assertEqual(self._cli("apply", "--rate", "5000")[0], 0)
        late = os.path.join(self.root, "Movies", "Lanternfall (2025)", ".DS_Store")
        put(late, ds_store(b"written later"))
        code, out = self._cli("apply", "--rate", "5000")
        self.assertEqual(code, 0, out)
        self.assertIn("nothing to rename", out)
        self.assertFalse(os.path.lexists(late))
        rows, kept = self._rows(2)
        self.assertEqual([(r["kind"], r["src"], r["status"]) for r in rows],
                         [("delete", late, "done")])
        self.assertIn(rows[0]["id"], kept)

    def test_a_lost_reply_is_settled_by_the_disk(self):
        real = os.unlink

        def gone_but_failed(path, *args, **kwargs):
            real(path, *args, **kwargs)
            raise TimeoutError("the reply was lost")

        with mock.patch.object(executor.os, "unlink", gone_but_failed):
            code, out = self._cli("clean")
        self.assertEqual(code, 0, out)
        rows, _ = self._rows()
        self.assertEqual({r["status"] for r in rows}, {"done"})
        self.assertTrue(all(r["error"].startswith("took effect although it reported")
                            for r in rows))
        self.assertEqual(self._cli("undo")[0], 0)
        self.assert_back_as_before()

    def test_a_delete_that_fails_leaves_the_file_and_undo_has_nothing_to_do(self):
        def refuse(path, *args, **kwargs):
            raise PermissionError(13, "Permission denied", path)

        with mock.patch.object(executor.os, "unlink", refuse):
            code, out = self._cli("clean")
        self.assertEqual(code, 1, out)
        self.assertEqual(snapshot(self.root), self.before)
        code, out = self._cli("undo")
        self.assertEqual(code, 0, out)
        self.assertIn("undone: 0 reversed, 0 failed", out)
        self.assert_back_as_before()

    def _cut_off(self, after_the_delete: bool):
        real = executor._do

        def do(kind, *args):
            if kind == "delete" and not after_the_delete:
                raise KeyboardInterrupt
            real(kind, *args)
            if kind == "delete":
                raise KeyboardInterrupt

        with mock.patch.object(executor, "_do", do):
            code, out = self._cli("clean")
        self.assertEqual(code, 130, out)
        rows, kept = self._rows()
        self.assertEqual([r["status"] for r in rows], ["started", "pending"])
        self.assertIn(rows[0]["id"], kept, "its bytes are in the journal before it goes")
        return rows[0]["src"]

    def test_cut_off_before_the_delete(self):
        self.assertTrue(os.path.lexists(self._cut_off(after_the_delete=False)))
        self.assertEqual(self._cli("undo")[0], 0)
        self.assert_back_as_before()

    def test_cut_off_after_the_delete_before_its_mark(self):
        self.assertFalse(os.path.lexists(self._cut_off(after_the_delete=True)))
        code, out = self._cli("undo")
        self.assertEqual(code, 0, out)
        self.assert_back_as_before()

    def test_undo_never_writes_over_what_is_at_the_path_now(self):
        self.assertEqual(self._cli("clean")[0], 0)
        again = ds_store(b"finder wrote this again")
        put(self.store, again)
        code, out = self._cli("verify", "--run", "1")
        self.assertEqual(code, 0, out)      # a new file there is no fault of the run
        code, out = self._cli("undo")
        self.assertEqual(code, 1, out)
        self.assertIn("something else is at", out)
        with open(self.store, "rb") as fh:
            self.assertEqual(fh.read(), again)
        os.remove(self.store)
        code, out = self._cli("undo")
        self.assertEqual(code, 0, out)
        self.assert_back_as_before()

    def test_a_move_waits_while_the_delete_after_it_cannot_be_undone(self):
        self.assertEqual(self._cli("apply", "--rate", "5000")[0], 0)
        remount(self.root)          # every number new: undo knows files by name, kind and size
        hidden = os.path.join(self.root, "Movies", "Lanternfall (2025)",
                              ".YTS.MX - Official site.jpg")
        put(hidden, b"x" * 32)      # someone else's file, of the ad's name and size
        code, out = self._cli("undo")
        self.assertEqual(code, 1, out)
        with open(hidden, "rb") as fh:
            self.assertEqual(fh.read(), b"x" * 32)
        self.assertFalse(os.path.lexists(self.ad))

    def test_one_failed_lookup_in_undo_leaves_nothing_behind(self):
        self.assertEqual(self._cli("apply", "--rate", "5000")[0], 0)
        hidden = os.path.join(self.root, "Movies", "Lanternfall (2025)",
                              ".YTS.MX - Official site.jpg")
        real, calls = junk.look, []

        def hiccup(path):
            if path == hidden and not calls:
                calls.append(path)
                raise TimeoutError(60, "Operation timed out", path)
            return real(path)

        with mock.patch.object(junk, "look", hiccup):
            code, out = self._cli("undo")
        self.assertEqual(code, 1, out)
        self.assertTrue(os.path.isdir(os.path.dirname(hidden)), "its folder waits for it")
        code, out = self._cli("undo")
        self.assertEqual(code, 0, out)
        self.assert_back_as_before()

    def test_a_file_is_not_written_back_into_a_folder_that_is_gone(self):
        self.assertEqual(self._cli("clean")[0], 0)
        film = os.path.join(self.root, FILM)
        os.rename(film, film + " moved")
        code, out = self._cli("undo")
        self.assertEqual(code, 1, out)
        self.assertIn("its folder is not there", out)
        self.assertFalse(os.path.exists(film), "not made again")
        os.rename(film + " moved", film)
        code, out = self._cli("undo")
        self.assertEqual(code, 0, out)
        self.assert_back_as_before()

    def test_after_a_clean_is_undone_an_older_run_undoes_at_once(self):
        # run 1 moves and hides, as the tool did before it deleted anything
        with mock.patch.object(cli, "delete_junk", lambda conn, run_id, args: 0):
            self.assertEqual(self._cli("apply", "--rate", "5000")[0], 0)
        code, out = self._cli("clean")                      # run 2
        self.assertEqual(code, 0, out)
        self.assertIn("to delete: 1 .DS_Store file, 1 torrent-site ad", out)
        code, out = self._cli("undo", "--run", "2")
        self.assertEqual(code, 0, out)
        conn = db.connect(self.dbpath)
        try:
            back = {r["src"] for r in db.run_ops(conn, 2) if r["kind"] == "delete"}
            hides = [r for r in db.run_ops(conn, 1) if r["dst"] in back]
            self.assertEqual(len(hides), 2)
            # written back with numbers of their own, they do not vote on whether
            # run 1's numbers still hold, even in a run that only hid such files
            self.assertFalse(executor.inodes_are_stale(hides, back))
        finally:
            conn.close()
        with mock.patch.object(undo, "inodes_are_stale",
                               wraps=executor.inodes_are_stale) as vote:
            code, out = self._cli("undo", "--run", "1")
        self.assertEqual(code, 0, out)
        self.assertEqual(set(vote.call_args.args[1]), back)
        self.assert_back_as_before()

    def test_verify_notices_a_deleted_file_that_is_back(self):
        self.assertEqual(self._cli("clean")[0], 0)
        put(self.ad, b"d" * 32)
        code, out = self._cli("verify", "--run", "1")
        self.assertEqual(code, 1, out)
        self.assertIn(f"still there: {self.ad}", out)


class Changed(LibraryCase):
    """A file that is not what the listing saw is not deleted."""

    def _journal(self):
        found, _ = junk.find(self.root, CATS)
        conn = db.connect(self.dbpath)
        run_id = db.create_run(conn, self.root, CATS, "clean")
        db.insert_ops(conn, run_id, junk.ops(found))
        return conn, run_id

    def test_a_file_that_grew_is_not_deleted(self):
        ad = os.path.join(self.root, AD)
        conn, run_id = self._journal()
        try:
            with open(ad, "ab") as fh:
                fh.write(b" and more")
            self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (0, 1))
            row = db.run_ops(conn, run_id)[0]
            self.assertEqual(row["status"], "failed")
            self.assertTrue(row["error"].startswith(executor.NOT_TRIED + "PlanError: size"),
                            row["error"])
            self.assertEqual(db.kept_of_run(conn, run_id), {})
            self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (0, 0))
        finally:
            conn.close()
        with open(ad, "rb") as fh:
            self.assertTrue(fh.read().endswith(b" and more"))

    def test_its_bytes_reach_the_disk_before_the_file_goes(self):
        conn, run_id = self._journal()
        seen, real = [], os.unlink

        def unlink(path, *args, **kwargs):
            seen.append("UNLINK")
            return real(path, *args, **kwargs)

        conn.set_trace_callback(seen.append)
        try:
            with mock.patch.object(executor.os, "unlink", unlink):
                self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (1, 0))
        finally:
            conn.close()
        before = seen[:seen.index("UNLINK")]
        on = max(i for i, s in enumerate(before) if s == "PRAGMA fullfsync=ON")
        self.assertIn("COMMIT", before[on:])

    def test_a_folder_move_waits_for_the_file_deleted_inside_it(self):
        old = os.path.join(self.root, "Movies", "Hollow Tide Extras")
        new = os.path.join(self.root, "Movies", ".Hollow Tide Extras")
        put(os.path.join(old, "RARBG.txt"), b"an ad " * 5)
        put(os.path.join(old, "notes.mkv"), b"v" * 40)
        before, before_dirs = snapshot(self.root), dirs(self.root)
        conn = db.connect(self.dbpath)
        try:
            run_id = db.create_run(conn, self.root, CATS, "test")
            db.insert_ops(conn, run_id, [Op(1, "rename", old, new, True, 0,
                                            db.inode_of(os.lstat(old)), "Movies", "hidden")])
            self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (1, 0))
            found = [j for j in junk.find(self.root, ["Movies"])[0]
                     if j.path.startswith(new + os.sep)]
            db.append_ops(conn, run_id, junk.ops(found))
            self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (1, 0))
            ad, real, calls = os.path.join(new, "RARBG.txt"), junk.look, []

            def hiccup(path):
                if path == ad and not calls:
                    calls.append(path)
                    raise TimeoutError(60, "Operation timed out", path)
                return real(path)

            with mock.patch.object(junk, "look", hiccup):
                self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (0, 2))
            self.assertTrue(os.path.isdir(new), "the folder waits for its file")
            self.assertEqual(undo.undo_run(conn, run_id, quiet=True), (2, 0))
        finally:
            conn.close()
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(dirs(self.root), before_dirs)

    def test_a_file_put_in_its_place_is_not_deleted(self):
        ad = os.path.join(self.root, AD)
        conn, run_id = self._journal()
        try:
            os.rename(ad, ad + ".was")
            put(ad, b"d" * 32)              # its name and size, another file
            self.assertEqual(executor.apply_ops(conn, run_id, quiet=True), (0, 1))
            row = db.run_ops(conn, run_id)[0]
            self.assertTrue(row["error"].startswith(executor.NOT_TRIED + "PlanError: inode"),
                            row["error"])
        finally:
            conn.close()
        self.assertTrue(os.path.lexists(ad))


if __name__ == "__main__":
    unittest.main(verbosity=2)
