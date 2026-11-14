# Safety guarantees

What plex-renamer promises about your library, in full; the [README](https://github.com/agusgonzaleznic/plex-renamer/blob/main/README.md) has the short version.

- **Two kinds of file are deleted, and undo writes them back.** These are
  torrent-site ads (a text, picture, link, page, info or program file whose
  name starts with the name of a torrent site, such as
  `YTS.MX - Official site.jpg`) and `.DS_Store` files, known by name and by the
  first bytes of that format. A poster or an info file named after a release
  only carries a site's tag, so a name with a year, a resolution, a codec or a
  source in it is never an ad. Each is a plain
  file of at most 1 MiB, and its bytes, modification time and mode go into the
  journal before `os.unlink` removes it, so `undo` writes it back. A file that
  has changed since it was listed is not deleted. The search for them never
  looks into folders whose names start with `@`, `#` or `.@`, which a NAS keeps
  for itself. The renamer leaves the `.@` ones alone too, but not yet the `@`
  and `#` ones: it plans moves for the files in them like for any other file
  (see "Known limitations" in the README).
- **Nothing else in the library is deleted.** Apart from those two kinds, the
  library is changed with `os.rename`, `os.mkdir`, `os.makedirs` and `os.rmdir`
  only, and `os.rmdir` only runs on a folder that a listing shows to be empty.
  A bare `rmdir` is no
  guard: macOS removes a folder that holds only `._` files, and the files with
  it. On a volume that keeps a folder's attributes in a `._` file beside the
  folder, such as FAT, macOS removes that file with the folder, so a folder with
  such a file beside it stays. The only things removed are empty directories:
  ones the plan clears away, which `undo` recreates, and ones a run created,
  once they are empty again. The plan clears away a folder the run empties, and
  an empty folder of a title that already has its proper folder. A folder that
  is already at a planned path when the run gets to it is not the run's, and
  `undo` leaves it. When a folder has to go, its folder metadata (`.DS_Store`,
  `Thumbs.db`) moves up to the parent first (as `.DS_Store-1` and so on when
  the name is taken). A `.DS_Store` is then deleted with the ads, and a
  `Thumbs.db` stays. A server can keep files of its own hidden
  from the share, such as thumbnail caches. The tool never sees those, and what
  becomes of them when their folder is removed is up to the server.
- **Besides those, the one thing the tool deletes is its own.** To estimate the running time,
  `apply` makes a new folder at the volume root (`.plexrenamer-benchmark-` and a
  random suffix), writes 60 scratch files into it, times renaming them and
  removes those files and that folder again, also on `--dry-run`. It removes
  nothing it did not make there, and a folder that holds anything else stays.
  `--rate` skips it.
- **A lookup that fails is not an answer.** On a share, a lookup can fail for a
  moment. Right before an operation and in `undo`, the tool never takes that to
  mean that a path is free or that a file is gone: the operation in hand fails
  and says why. While the plan is made, such a lookup can make the plan refuse
  to start or leave something out, and the checks right before each operation
  catch the rest. When `undo` or `verify` cannot read the library to look a run
  over, it stops, changes nothing and says so. Only `undo` checks first that the
  library is there: with the library away, `verify` reports the moved files as
  mismatched, and the files the run deleted as confirmed.
- **Every run can be undone, also one that was cut off, and also after a remount.**
  The whole plan is in the SQLite journal before the first operation, each rename
  with the size and inode of its source, each deleted file with its bytes before
  it goes, and each operation is marked as started
  before it runs and as done after. `undo` goes through the journal backwards and
  settles every row by where its file is now, found by name, kind and size. That
  way `undo` also reverses an operation that was in flight when the run stopped
  (Ctrl-C, a kill, a crash), one whose mark never reached the journal, and a
  rename that went through although the share reported an error. The inode is
  extra proof while the journal's numbers still tell files apart. A network
  share can number its files anew on every mount, or work each number out from
  the path, so that every rename changes it. The files the run moved show
  whether it has, and then the inode is not asked for. On such a share a rename
  whose reply was lost needs a size to go by: for a folder or an empty file it
  is reported and stays as it is. When `apply` or `prune` moves folder
  metadata, that move is in the journal too.
- **Undo does not guess and does not give up.** It never moves a file onto an
  original path that has been taken, moves back only what it finds by name, kind
  and size where the run was to put it, and keeps a folder a run created while
  anything else is in it. A rename that `apply` refused before trying it,
  because its source was gone or its destination taken, never touched the disk,
  and `undo` leaves it alone. It stops when the library is not mounted. A row it
  could not undo keeps its state and is tried again by the next `undo`, which is
  also how an `undo` that was itself cut off is finished. A run is marked undone
  only when every row is reversed or shown not to have happened, and `undo` then
  leaves it alone. Runs are undone newest first: plain `undo` picks the newest
  run that still stands, and `undo --run N` refuses while a later one does,
  unless `--force` is given.
- **What undo cannot do.** It cannot bring back what is no longer there: a file
  that was deleted, moved elsewhere or replaced since the run stays as a failed
  row, and the run stays partly undone. So does a folder the run removed whose
  name something else has taken since: it is not made again. A deleted file is
  written back only where its path is free: a file there now, such as a
  `.DS_Store` Finder has written again, stays, and the row waits with the bytes
  in the journal. Until a deleted file is back, the rows that would move or
  remove its folder wait too, and a file whose folder is gone is not written
  back: the folder is not made again. An `undo` that is killed while it writes
  a file back can leave a `.plexrenamer-restore-` file beside it, which keeps
  that folder in place until it is removed by hand. An older run can
  then still be undone by naming it with `--run` and adding `--force`; its rows
  on paths the later run still stands on wait until that run is undone. A
  folder's own attributes, such as Finder tags or a comment, go with it when it
  is removed: `undo` recreates the folder without them. A folder and an empty
  file are known by name and kind alone. Where the inode is not asked, two
  things of one kind and size cannot be told apart either:
  - a file of the same name, kind and size cannot be told from the one the run
    moved;
  - a move of a folder or an empty file that went through although the journal
    has it as failed is reported, not reversed;
  - where a run was to move such look-alikes along one path (a swap, or a to b
    and b to c), the journal decides: a row behind the last row that carries a
    mark never ran, and a rename refused before it was tried did not happen. A
    leg that failed but may have gone through all the same, as after a lost
    reply, is reported and stays as it is. In a journal written before refused
    renames were marked as such, so is every failed leg. Each mark is committed
    on its own, so marks go missing only when the journal loses writes it had
    committed: in a power cut, or when it is copied without its `-wal` file. A
    swap or a cycle of look-alikes whose marks went missing can look as if it
    never got that far. Undo then leaves some or all of it where the run put
    it, and marks the run undone all the same.
- **The plan is validated before anything moves.** Two sources mapping to one
  destination, a vanished source, or an already-occupied destination all stop
  the run before it starts, unless `--force` is given. Even then nothing is
  overwritten: right before each rename its destination is looked at again, and
  a taken one fails that row. Each file is also looked at again right before its
  own move: one that has changed kind or size since the plan, or is no longer
  the planned file by its inode where that tells, stays where it is. A file
  planned empty is checked by kind alone.
- **One writer at a time.** The checks look at a path and then rename it, and
  nothing locks the library. Run the tool while nothing else writes to it.
- **Pruning cannot escape the library.** Empty-directory removal is bounded to the
  interior of the category directories, so it can never reach the volume root.
- **Renames are ordered.** A move whose destination is still occupied by another
  pending move is deferred; a genuine swap is staged through a temporary name.
