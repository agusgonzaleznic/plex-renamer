# plex-renamer

Renames a local media volume to the naming standard Plex publishes, with a full
SQLite journal so any run can be replayed in reverse.

The rules are taken from Plex's own support articles. The distilled ruleset is
kept in `reports/plex-spec.json` as a reference, with the source article for each
section; the code does not read it. Of its 260 rules, 155 are marked
`verified: true` and 105 `verified: false`.

## Guarantees

- **Nothing in the library is deleted.** The library is changed with
  `os.rename`, `os.mkdir`, `os.makedirs` and `os.rmdir` only, and `os.rmdir`
  only runs on a folder that a listing shows to be empty. A bare `rmdir` is no
  guard: macOS removes a folder that holds only `._` files, and the files with
  it. On a volume that keeps a folder's attributes in a `._` file beside the
  folder, such as FAT, macOS removes that file with the folder, so a folder with
  such a file beside it stays. The only things removed are empty directories:
  ones the plan clears away, which `undo` recreates, and ones a run created,
  once they are empty again. The plan clears away a folder the run empties, and
  an empty folder of a title that already has its proper folder. A folder that
  is already at a planned path when the run gets to it is not the run's, and
  `undo` leaves it. Folder metadata (`.DS_Store`, `Thumbs.db`) is not deleted
  either: when its folder has to go, it moves up to the parent (as `.DS_Store-1`
  and so on when the name is taken). A server can keep files of its own hidden
  from the share, such as thumbnail caches. The tool never sees those, and what
  becomes of them when their folder is removed is up to the server.
- **The one thing the tool deletes is its own.** To estimate the running time,
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
  over, it stops, changes nothing and says so.
- **Every run can be undone, also one that was cut off, and also after a remount.**
  The whole plan is in the SQLite journal before the first operation, each rename
  with the size and inode of its source, and each operation is marked as started
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
  name something else has taken since: it is not made again. An older run can
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

## Usage

```bash
python3 -m plexrenamer plan                       # build and validate, write reports/
python3 -m plexrenamer apply --dry-run            # op count, measured rate, ETA only
python3 -m plexrenamer apply                      # run it, with a progress bar
python3 -m plexrenamer verify                     # re-check the last run by name and size
python3 -m plexrenamer undo                       # reverse the newest run that stands
python3 -m plexrenamer undo --run 3               # reverse a specific run
python3 -m plexrenamer status                     # list runs and their op counts
python3 -m plexrenamer prune --dry-run            # find shells emptied by a run
```

Useful flags: `--root` (default `/Volumes/Multimedia`), `--categories` to limit the
run, `--episode-case lower` to emit `s01e01` instead of `S01E01`, `--decisions` to
point at a different override directory.

## How it works

1. **scan** walks the category directories read-only into a tree.
2. **classify** decides whether each folder is a movie, a show, or a grouping
   folder holding several titles, and flags anything it is unsure about.
3. **decisions** overlays per-title corrections from `decisions/*.json`: canonical
   title, year, database ids, movie/show, editions, or skip.
4. **planner** turns each title into concrete file moves.
5. **executor** validates, expands into `mkdir` / `rename` / `rmdir` operations,
   journals them, runs them with a progress bar, then checks that every file is
   where the journal says, by name, kind and size.
6. **undo** settles every journal row against the disk, newest first.

## Naming applied

| Case | Result |
|---|---|
| Movie | `Category/Title (Year) {imdb-tt…}/Title (Year) {imdb-tt…}.ext` |
| Movie edition | `Title (Year) {edition-Final Cut}/Title (Year) {edition-Final Cut}.ext` |
| Split movie | `Title (Year) - part1.ext` … `part8` |
| Show | `Category/Show (Year) {imdb-tt…}/Season 01/Show (Year) - S01E01 - Title.ext` |
| Multi-episode file | `Show (Year) - S02E01-E03.ext` |
| Specials | `Specials/Show (Year) - S00E01 - Title.ext` |
| Subtitle | `<video stem>.en.srt`, `.en.forced.srt`, `.en.sdh.srt` |
| Extras | `Behind The Scenes`, `Deleted Scenes`, `Featurettes`, `Interviews`, `Scenes`, `Shorts`, `Trailers`, `Other` |
| Artwork | `poster.jpg`, `background.jpg` |
| DVD rip | `Title (Year)/VIDEO_TS/…` kept intact |
| Junk | dot-prefixed so the scanner skips it, never removed |

## Deliberate deviations from the published examples

- **`S01E01`, not `s01e01`.** Plex's examples are lowercase and the token is
  matched case-insensitively. Uppercase matches what every mainstream tool
  already uses. `--episode-case lower` switches it.
- **A regional label before the language code** (`Movie (2019).Brazilian.pt.srt`).
  Plex documents only `name.lang.ext`, which would collide when a release ships
  several variants of one language. The label form is widely supported and is the
  only way to keep the distinction. It is only used from the third file of a
  language on: the first two take the two code widths Plex documents, `.pt.srt`
  and `.por.srt`, so `.eng.srt` beside `.en.srt` is intended.
- **A three-letter tag the tool does not know is kept as the language code.**
  Release tools name subtitle files by ISO-639-2 code, so `xyz.srt` becomes
  `Movie (2019).xyz.srt`, and lint lists every such code. A name with no
  language in it, or with `und` (undetermined), is filed as English.
- **One id tag per title.** Plex documents `{imdb-…}`, `{tmdb-…}` and `{tvdb-…}`
  as alternatives, so exactly one is emitted, preferring IMDb.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Covers the naming patterns, plan validation, cycle breaking, the apply/undo
round trip byte for byte, and an explicit assertion that no file content ever
disappears from the volume. The recovery tests cut `apply` and `undo` off at
every single step, before and after the step touches the disk, and check that
the tree comes back exactly, folder metadata included, also when the tree has
been swapped for a copy with new inode numbers in between, which is what a
remount does. The safety tests put somebody else's files in the way, take the
library away, lose a reply from the share and undo runs out of order, and check
that no foreign file is touched and that every row that could not be undone is
still there for the next try. The share tests put the library on the inode
numbers an SMB share was seen to hand out on macOS: numbers above 2^63 - 1,
which are more than an SQLite integer holds, and numbers worked out from the
path, which change with every rename. On the second kind the round trip,
recovery and safety tests run a second time, apart from two safety tests whose
promise rests on an inode that holds: there each is replaced by a test of what
is left of it. The look-alike tests do the same with swaps and chains of files
of one size, folders and empty files, cut off at every step, and undo an older
run with `--force` while a later run on its paths still stands. The lookup tests
make single lookups fail where a decision rests on them, and remove folders the
way macOS does, also on a FAT volume.

## Pruning shells left behind by a rename

```bash
python3 -m plexrenamer prune --dry-run     # list what would go
python3 -m plexrenamer prune               # remove them, journalled
```

`prune` removes directories that hold no media anywhere below them, but only
those this tool actually emptied, established from the journal rather than
guessed. A folder that was already empty before the tool ever ran is reported
and left alone. Any folder metadata inside a pruned directory is moved to the
parent rather than deleted, and `undo` recreates the directory and puts that
file back. A directory that still holds anything else, `._` files included, is
kept and reported.
