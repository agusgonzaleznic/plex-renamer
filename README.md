# plex-renamer

Renames a local media volume to the naming standard Plex publishes, with a full
SQLite journal so any run can be replayed in reverse.

The rules are taken from Plex's own support articles. The distilled ruleset is
kept in `reports/plex-spec.json` as a reference, with the source article for each
section; the code does not read it. Of its 260 rules, 155 are marked
`verified: true` and 105 `verified: false`.

## Guarantees

- **Nothing in the library is deleted.** The library is changed with `os.rename`,
  `os.makedirs` and `os.rmdir` only. The only things removed are directories: ones
  the tool emptied, which `undo` recreates, and ones a run created, once they are
  empty again. A folder that appears at a planned path before the run gets to it
  counts as created by the run. Folder metadata (`.DS_Store`, `Thumbs.db`) is not
  deleted either: when its folder has to go, it moves up to the parent (as
  `.DS_Store-1` and so on when the name is taken).
- **The one thing the tool deletes is its own.** To estimate the running time,
  `apply` writes 60 scratch files to `<root>/.plexrenamer-benchmark`, times
  renaming them and removes them and that folder again, also on `--dry-run`.
  `--rate` skips it.
- **Every run can be undone, also one that was cut off, and also after a remount.**
  The whole plan is in the SQLite journal before the first operation, each rename
  with the size and inode of its source, and each operation is marked as started
  before it runs and as done after. `undo` goes through the journal backwards and
  settles every row by where its file is now, found by name, kind and size. The
  inode is extra proof while the journal's numbers still hold; a network share
  can number its files anew on every mount, and then it is not asked for. That
  way `undo` also reverses an operation that was in flight when the run stopped
  (Ctrl-C, a kill, a crash), one whose mark never reached the journal, and a
  rename that went through although the share reported an error. When `apply` or
  `prune` moves folder metadata, that move is in the journal too.
- **Undo does not guess and does not give up.** It never moves a file onto an
  original path that has been taken, moves back only what it finds by name, kind
  and size where the run was to put it, and keeps a folder a run created while
  anything else is in it. It stops when the library is not mounted. A row it
  could not undo keeps its state and is tried again by the next `undo`, which is
  also how an `undo` that was itself cut off is finished. Runs are undone newest
  first: plain `undo` picks the newest run that still stands, and `undo --run N`
  refuses while a later one does, unless `--force` is given.
- **What undo cannot do.** It cannot bring back what is no longer there: a file
  that was deleted, moved elsewhere or replaced since the run stays as a failed
  row, and the run stays partly undone. A folder, and a file whose size the
  journal does not hold, is known by name and kind alone.
- **The plan is validated before anything moves.** Two sources mapping to one
  destination, a vanished source, or an already-occupied destination all abort the
  run rather than overwrite.
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
   journals them, runs them with a progress bar, then verifies inodes on disk.
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
  only way to keep the distinction.
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
still there for the next try.

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
file back.
