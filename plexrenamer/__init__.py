"""Plex-standard renamer for a local media volume.

Every mutation goes through a SQLite journal so that any run can be replayed
in reverse. Nothing in the library is ever deleted: the only things removed are
directories this tool itself emptied or created.
"""

__version__ = "1.0.0"
