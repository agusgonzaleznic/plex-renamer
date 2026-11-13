"""Torrent-site ads and .DS_Store files: the two kinds of file the tool deletes.

Everything else in the library is only renamed. These two may go, and even so a
deletion can be reversed: before a file is removed, its bytes, its modification
time and its mode go into the journal, and undo writes it back from there. That
is why only small files are deleted (MAX_BYTES), and why a file named like a
.DS_Store counts as one only when it starts with that format's magic bytes.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass

from . import db
from .ops import Op, PlanError

# An ad is a text, picture, link, page, info or program file whose name starts
# with the name of a torrent site. A poster or an info file named after a release
# only carries a site's tag, so a name with the marks of a release in it (a year,
# a resolution, a codec, a source) is never one.
AD_EXTS = {".txt", ".jpg", ".png", ".url", ".html", ".nfo", ".exe"}
AD_START = re.compile(r"(?i)(?:www\.)?(?:yts|yify|rarbg)|torrent[ ._-]downloaded[ ._-]from")
RELEASE = re.compile(r"(?i)(?:19|20)\d\d|\d{3,4}p|[xh]\.?26[45]|hevc|"
                     r"(?:br|bd|dvd|hd|web)-?rip|web-?dl|blu-?ray")
# .DS_Store; the numbered copies left where one moved up out of a removed folder
# (.DS_Store-1); and the names a hiding or an older rename gave one (.DS Store,
# .hidden-DS_Store, DS Store).
DS_STORE_RE = re.compile(r"(?i)\.?(?:hidden-)?\.?ds[ _]store(?:-\d+)*")
DS_STORE_MAGIC = b"\x00\x00\x00\x01Bud1"
MAX_BYTES = 1 << 20
# Folders a NAS keeps for itself (recycle bins, snapshots, thumbnails): never looked into.
NAS_OWN = ("@", "#", ".@")
AD = "torrent-site ad"
DS_STORE = ".DS_Store file"
RESTORE_PREFIX = ".plexrenamer-restore-"


@dataclass
class Junk:
    path: str
    size: int
    inode: int
    category: str
    reason: str


def kind(name: str):
    """AD or DS_STORE for a file name the tool may delete, else None.

    A name that starts with "._" holds the attributes of another file on some
    volumes and never counts.
    """
    if name.startswith("._"):
        return None
    if DS_STORE_RE.fullmatch(name):
        return DS_STORE
    bare = re.sub(r"(?i)^\.(?:hidden-)?", "", name)
    stem, ext = os.path.splitext(bare)
    if ext.lower() in AD_EXTS and AD_START.match(bare) and not RELEASE.search(stem):
        return AD
    return None


def look(path: str):
    """The lstat of path, or None when nothing is there. Any other error is
    raised: on a share a hiccup must never pass for a path that is free."""
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None


def _starts_with(path: str, magic: bytes) -> bool:
    with open(path, "rb") as fh:
        return fh.read(len(magic)) == magic


def find(root: str, categories):
    """What may be deleted inside the category folders, as a list of Junk, and
    what was left although named like it, as a list of (path, why)."""
    found, left = [], []
    for category in categories:
        def unlisted(exc, left=left):
            left.append((getattr(exc, "filename", "") or "?", f"could not be listed: {exc}"))
        for current, dirs, files in os.walk(os.path.join(root, category), onerror=unlisted):
            dirs[:] = sorted(d for d in dirs if not d.startswith(NAS_OWN))
            for name in sorted(files):
                reason = kind(name)
                if reason is None:
                    continue
                path = os.path.join(current, name)
                try:
                    st = os.lstat(path)
                    why = None
                    if not stat.S_ISREG(st.st_mode):
                        why = "not a plain file"
                    elif st.st_size > MAX_BYTES:
                        why = f"{st.st_size} bytes, too big to keep in the journal"
                    elif reason == DS_STORE and not _starts_with(path, DS_STORE_MAGIC):
                        why = "named like a .DS_Store, but it is not one"
                except OSError as exc:
                    why = f"could not be read: {type(exc).__name__}: {exc}"
                if why:
                    left.append((path, why))
                else:
                    found.append(Junk(path, st.st_size, db.inode_of(st), category, reason))
    return found, left


def ops(found, start: int = 0):
    """Delete operations for what find returned, numbered on from start."""
    return [Op(start + i, "delete", j.path, None, False, j.size, j.inode, j.category, j.reason)
            for i, j in enumerate(found, 1)]


def keep(conn, row, inode: int) -> None:
    """Put the bytes of the file a delete row removes into the journal.

    It has to be the file that was planned: still a plain file of the planned
    size, and of the planned inode where `inode` is given. Anything else raises,
    and then nothing is deleted. The caller commits before the file goes.
    """
    path = row["src"]
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise PlanError(f"no longer a plain file: {path}")
    if st.st_size != row["size"] or st.st_size > MAX_BYTES:
        raise PlanError(f"size changed for {path}: planned {row['size']} bytes, "
                        f"now {st.st_size}")
    if inode and db.inode_of(st) != inode:
        raise PlanError(f"inode changed for {path}: expected {inode}, saw {db.inode_of(st)}")
    with open(path, "rb") as fh:
        data = fh.read(MAX_BYTES + 1)
    if len(data) != st.st_size:
        raise PlanError(f"changed while it was read: {path}")
    db.keep_bytes(conn, row["id"], hashlib.sha256(data).hexdigest(), st.st_mtime_ns,
                  stat.S_IMODE(st.st_mode), data)


def same_bytes(path: str, kept) -> bool:
    """Whether path is a plain file that holds exactly the kept bytes."""
    st = look(path)
    if st is None or not stat.S_ISREG(st.st_mode) or st.st_size != len(kept["data"]):
        return False
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest() == kept["sha256"]


def restore(path: str, kept) -> None:
    """Write a deleted file back from the journal, never over anything.

    The bytes go to a new file of the tool's own beside it first, made under a
    name no file has, which takes the name only while the name is free. The
    modification time and the mode are set back where the volume allows it.
    """
    parent = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=f"{RESTORE_PREFIX}{kept['op_id']}-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(kept["data"])
            fh.flush()
            os.fsync(fh.fileno())
        with contextlib.suppress(OSError):
            os.chmod(tmp, kept["mode"])
        with contextlib.suppress(OSError):
            os.utime(tmp, ns=(kept["mtime_ns"], kept["mtime_ns"]))
        if look(path) is not None:
            raise PlanError(f"something else is at {path} now, so it is not written back")
        os.rename(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    if not same_bytes(path, kept):
        raise PlanError(f"written back, but it does not read back the same: {path}")


def still_there(row, kept):
    """What verify finds wrong with a delete the journal has as done, or None.

    Something new at the path is no fault of the run: Finder writes a .DS_Store
    again where it likes. Only the very bytes that were deleted count.
    """
    if kept is None:
        return f"no copy of it in the journal: {row['src']}"
    if same_bytes(row["src"], kept):
        return f"still there: {row['src']}"
    return None
