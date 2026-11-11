"""Walk the volume into an in-memory tree. Read-only."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import util


@dataclass(eq=False)
class Node:
    path: str
    rel: str
    name: str
    is_dir: bool
    size: int = 0
    inode: int = 0
    children: list = field(default_factory=list)
    parent: "Node | None" = None

    @property
    def stem(self) -> str:
        return os.path.splitext(self.name)[0] if not self.is_dir else self.name

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1].lower() if not self.is_dir else ""

    @property
    def is_video(self) -> bool:
        return not self.is_dir and self.ext in util.VIDEO_EXT

    @property
    def is_subtitle(self) -> bool:
        return not self.is_dir and self.ext in util.SUBTITLE_EXT

    @property
    def is_image(self) -> bool:
        return not self.is_dir and self.ext in util.IMAGE_EXT

    @property
    def is_hidden(self) -> bool:
        return self.name.startswith(".")

    def videos(self):
        return [c for c in self.children if c.is_video]

    def dirs(self):
        return [c for c in self.children if c.is_dir and not c.is_hidden]

    def files(self):
        return [c for c in self.children if not c.is_dir and not c.is_hidden]

    def walk(self):
        yield self
        for c in self.children:
            if c.is_dir:
                yield from c.walk()
            else:
                yield c


# Names that are pure noise and should be hidden from Plex rather than renamed.
JUNK_NAME_HINTS = (
    "yts", "yify", "yifystatus", "ytsproxies", "rarbg", "www.", "downloaded from",
    "torrent downloaded", "read me", "readme", "info.txt", "ds store",
)


def is_junk(node: Node) -> bool:
    low = node.name.lower()
    if low in {"ds store", "ds_store", "thumbs.db", "desktop.ini"}:
        return True
    if node.is_dir:
        return False
    if node.ext in {".txt", ".jpg", ".png", ".url", ".html", ".nfo", ".exe"}:
        return any(h in low for h in JUNK_NAME_HINTS)
    return False


def scan(root: str, categories) -> dict:
    """Return {category: Node} for the given category directories."""
    out = {}
    for cat in categories:
        base = os.path.join(root, cat)
        if not os.path.isdir(base):
            continue
        out[cat] = _build(base, cat, None)
    return out


def _build(path: str, rel: str, parent) -> Node:
    st = os.lstat(path)
    node = Node(path=path, rel=rel, name=os.path.basename(path), is_dir=True,
                inode=st.st_ino, parent=parent)
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name)
    except OSError:
        return node
    for entry in entries:
        if entry.name in {".DS_Store"} or entry.name.startswith("._"):
            continue
        child_rel = f"{rel}/{entry.name}"
        try:
            if entry.is_dir(follow_symlinks=False):
                node.children.append(_build(entry.path, child_rel, node))
            else:
                est = entry.stat(follow_symlinks=False)
                node.children.append(Node(
                    path=entry.path, rel=child_rel, name=entry.name, is_dir=False,
                    size=est.st_size, inode=est.st_ino, parent=node))
        except OSError:
            continue
    return node


def count(node: Node):
    files = dirs = 0
    for n in node.walk():
        if n is node:
            continue
        if n.is_dir:
            dirs += 1
        else:
            files += 1
    return files, dirs
