# plex-renamer

Renames a local media volume to the naming standard Plex publishes, with a full
SQLite journal so any run can be replayed in reverse.

The rules are taken from Plex's own support articles. The distilled ruleset is
kept in `reports/plex-spec.json` as a reference, with the source article for each
section; the code does not read it. Of its 260 rules, 155 are marked
`verified: true` and 105 `verified: false`.

## Guarantees

- **Nothing is deleted.** Every operation is `os.rename`. The only removals are of
  directories this tool itself emptied, and `undo` recreates them.
- **Every run is reversible.** Each operation is written to SQLite before it runs
  and updated after, with the source inode recorded. `undo` replays the journal
  backwards and refuses to move a file whose original path has been reoccupied.
- **Three known exceptions to the above.** Folder metadata: before the tool tries
  to remove a directory it has emptied, a `.DS_Store` or `Thumbs.db` inside it is
  moved up to the parent folder (as `.DS_Store-1` and so on when the name is
  taken) without a journal entry, so `undo` recreates the directory but leaves
  that file in the parent; and when `undo` removes a directory the tool created,
  a `.DS_Store` that has appeared inside it since is removed with it. Interrupted
  runs: the journal marks are committed in batches of 200, so when `apply` is
  stopped part-way (Ctrl-C, a kill, a crash) the operations finished since the
  last batch are on disk but not marked as done, and `undo` skips them. Their
  source and destination paths remain in the journal. Failed rows: a row that
  `undo` cannot reverse is marked as failed and never tried again, and the next
  `undo` then reports the run as undone. With the library not mounted every
  rename fails that way, the folders the run created are written off as gone,
  and the folders it removed are recreated wherever that path can be written.
  Run `undo` only with the library mounted.
- **The plan is validated before anything moves.** Two sources mapping to one
  destination, a vanished source, or an already-occupied destination all abort the
  run rather than overwrite.
- **Pruning cannot escape the library.** Empty-directory removal is bounded to the
  interior of the category directories, so it can never reach the volume root.
- **Renames are ordered.** A move whose destination is still occupied by another
  pending move is deferred; a genuine swap is staged through a temporary name.

## Usage

```bash
python3 -m plexrenamer plan                       # build and validate, write reports/
python3 -m plexrenamer apply --dry-run            # op count, measured rate, ETA only
python3 -m plexrenamer apply                      # run it, with a progress bar
python3 -m plexrenamer verify                     # re-check the last run against disk
python3 -m plexrenamer undo                       # reverse the last run
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
disappears from the volume.

## Pruning shells left behind by a rename

```bash
python3 -m plexrenamer prune --dry-run     # list what would go
python3 -m plexrenamer prune               # remove them, journalled
```

`prune` removes directories that hold no media anywhere below them, but only
those this tool actually emptied, established from the journal rather than
guessed. A folder that was already empty before the tool ever ran is reported
and left alone. Any Finder metadata inside a pruned directory is moved to the
parent rather than deleted, and `undo` recreates the directory.
