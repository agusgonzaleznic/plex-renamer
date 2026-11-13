"""Name-shaping helpers used while planning: hiding, labels, subtitle flavour
and collision-free destination basenames."""

from __future__ import annotations

import os
import re

from . import naming, util

PART_RE = re.compile(r"(?i)[\s._-]+(?:part|pt|cd|disc|disk|dvd)[\s._-]*(\d{1,2})\s*$")

# Dotted names the system owns. Finder writes its own view settings into
# .DS_Store, macOS takes a "._" file for the attributes of the file or folder
# of the rest of its name and removes a folder that holds only such files, and
# file servers often hide both and delete them with their folder.
SYSTEM_NAME_RE = re.compile(r"(?is)\.(?:ds_store|_.*)")


def _hidden(name: str) -> str:
    """The name that hides a file or folder from the scanner: a dot in front.

    Where that gives a name the system owns, the file could be lost, so it
    gets ".hidden-" in front instead. A name that starts with "hidden-" gets
    it too, or "_x" and "hidden-_x" would both become ".hidden-_x".
    """
    if name.startswith("."):
        return name
    if SYSTEM_NAME_RE.fullmatch("." + name) or re.match(r"(?i)hidden-", name):
        return ".hidden-" + name
    return "." + name


def _free_path(path: str) -> str:
    """Pick a name that is not already taken on disk (used for hidden junk)."""
    if not os.path.lexists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 1
    while os.path.lexists(f"{stem}-{n}{ext}"):
        n += 1
    return f"{stem}-{n}{ext}"


def _label(stem: str) -> str:
    """Extras keep their descriptive name; only scene noise is stripped."""
    text = util.normalize_separators(stem)
    text = util.strip_bracket_junk(text)
    return util.sanitize(text) or stem


def _sub_flavour(sub_stem: str, video_stem: str):
    """Work out language/forced/sdh for a subtitle given its video's stem."""
    extra = sub_stem
    if video_stem and util.strip_accents_key(sub_stem).startswith(
            util.strip_accents_key(video_stem)):
        extra = sub_stem[len(video_stem):]
        if util.strip_accents_key(extra) == "":
            extra = ""
    return util.normalize_lang(extra)


def _sub_language(lang, leftover):
    """Decide the language code when the name gave none this tool knows.

    Plex ignores a sidecar without a language code, so one is always emitted.
    Release tools name subtitle files by ISO-639-2 code, so a bare three-letter
    tag that is not in the table ("fil" was one) is taken as the code it is,
    where it used to be kept as a label on a file called English. Lint lists
    every code the table does not know, so a tag that was no language shows up.
    """
    if lang:
        return lang, leftover
    tag = (leftover or "").lower()
    # "und" is the ISO code for undetermined: it says nothing, so English stays
    # the guess for it, as for a name with no tag at all
    if re.fullmatch(r"[a-z]{3}", tag) and tag != "und":
        return tag, ""
    return "en", leftover


def _unique_subtitle(used: dict, stem: str, lang, forced, sdh, leftover, ext: str,
                     current: str | None = None):
    """Pick a subtitle name Plex can still parse when one language repeats.

    A trailing " (2)" would be read as part of the language code, so the ladder
    is: two-letter code, then the ISO-639-2/B three-letter code, then a numeric
    title component placed before the language code.
    """
    # a leftover that is already part of the destination stem adds nothing
    if leftover and util.strip_accents_key(leftover) in util.strip_accents_key(stem):
        leftover = ""
    # Plex documents only <stem>.<lang>[.forced|.sdh].<ext>. Any extra component
    # risks the language not being read, so try both documented code widths first
    # and only then fall back to a descriptive label.
    candidates = [naming.subtitle_file(stem, lang, forced, sdh, None, ext)]
    three = util.ISO_639_2B.get(lang or "")
    if three and three != lang:
        candidates.append(naming.subtitle_file(stem, three, forced, sdh, None, ext))
    if leftover:
        candidates.append(
            naming.subtitle_file(stem, lang, forced, sdh, leftover, ext))
        if three and three != lang:
            candidates.append(
                naming.subtitle_file(stem, three, forced, sdh, leftover, ext))
    for n in range(2, 40):
        label = f"{leftover} {n}".strip() if leftover else str(n)
        candidates.append(naming.subtitle_file(stem, lang, forced, sdh, label, ext))
    # if the file already sits on a rung of this ladder, keep it there. Without
    # this the assignment depends on iteration order and a second run permutes
    # every subtitle of a language among themselves for no gain.
    if current and current in candidates and current not in used:
        used[current] = 1
        return current
    for candidate in candidates:
        if candidate not in used:
            used[candidate] = 1
            return candidate
    return _dedupe(used, candidates[0])


def _dedupe(names: dict, candidate: str) -> str:
    """Guarantee unique destination basenames inside one directory."""
    if candidate not in names:
        names[candidate] = 1
        return candidate
    stem, ext = os.path.splitext(candidate)
    n = names[candidate] + 1
    while f"{stem} ({n}){ext}" in names:
        n += 1
    names[candidate] = n
    out = f"{stem} ({n}){ext}"
    names[out] = 1
    return out


