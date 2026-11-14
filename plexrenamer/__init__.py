"""Plex-standard renamer for a local media volume.

Every mutation goes through a SQLite journal so that any run can be replayed
in reverse. The only files deleted from the library are torrent-site ads and
.DS_Store files, whose bytes go into the journal first so that undo writes them
back. Apart from those, the only things removed there are empty directories:
those a plan clears away, which undo recreates, and those a run created, once
they are empty again. The speed test removes only the scratch files it wrote
into a folder of its own.
"""

__version__ = "1.0.2"


def console():
    """The installed plex-renamer command: the commands it prints start with its own name."""
    from . import cli
    cli.PROG = "plex-renamer"
    return cli.main()
