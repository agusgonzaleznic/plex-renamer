"""Plex-standard renamer for a local media volume.

Every mutation goes through a SQLite journal so that any run can be replayed
in reverse. Nothing is ever deleted: the only removals performed are of
directories this tool itself emptied, and those are recreated on undo.
"""

__version__ = "1.0.0"
