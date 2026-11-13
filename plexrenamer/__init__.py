"""Plex-standard renamer for a local media volume.

Every mutation goes through a SQLite journal so that any run can be replayed
in reverse. Nothing in the library is ever deleted. The only things removed
there are empty directories: those a plan clears away, which undo recreates,
and those a run created, once they are empty again. The speed test removes
only the scratch files it wrote into a folder of its own.
"""

__version__ = "1.0.0"
