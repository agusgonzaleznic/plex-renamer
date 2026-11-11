"""Static checks on a built plan, so a bad rule is caught before anything moves."""

from __future__ import annotations

import collections
import os
import re

from . import util

RELEASE_LEFTOVER = re.compile(
    r"(?i)\b(1080p|720p|2160p|480p|x264|x265|hevc|webrip|web-dl|bluray|bdrip|"
    r"hdtv|aac\d?|ac3|eac3|ddp\d?|dts|yts|yify|rarbg|10bit)\b")
DUP_SUFFIX = re.compile(r" \((?!\d{4}\))\d+\)\.[A-Za-z0-9]+$")
EMPTY_SEGMENT = re.compile(r"-\s+-|\s-\s*\.[A-Za-z0-9]+$|\(\s*\)")
SUB_LANG = re.compile(r"\.[A-Za-z]{2,3}(\.(forced|sdh|cc))?\.[A-Za-z0-9]+$")
EPISODE = re.compile(r"(.*)/(Season \d+|Specials)/(.*) - S(\d+)E(\d+)")


def check(moves, root: str, categories):
    """Return {check name: [messages]}."""
    out = collections.defaultdict(list)
    # two source titles landing in one folder read as versions of one movie, which
    # is right sometimes and a silent merge of two different works otherwise
    folders = collections.defaultdict(set)
    for m in moves:
        if os.path.splitext(m.dst)[1].lower() in util.VIDEO_EXT:
            folders[os.path.dirname(m.dst)].add(m.unit)
    for folder, units in folders.items():
        if len(units) > 1:
            rel = folder[len(root) + 1:] if folder.startswith(root) else folder
            out["two source titles merged into one folder"].append(
                f"{rel}  <-  " + " + ".join(sorted(units)))
    roots = [os.path.join(root, c) for c in categories]
    seen_dst = collections.Counter(m.dst for m in moves)
    # a sidecar sitting next to an extra keeps that extra's stem, by design
    extras_stems = {os.path.splitext(os.path.basename(m.dst))[0] for m in moves
                    if os.path.splitext(m.dst)[1].lower() in util.VIDEO_EXT}

    def hidden(path: str) -> bool:
        return any(part.startswith(".") for part in path.split(os.sep))

    for m in moves:
        base = os.path.basename(m.dst)
        rel = m.dst[len(root) + 1:] if m.dst.startswith(root) else m.dst
        if hidden(rel):
            # deliberately hidden from the scanner, so naming rules do not apply
            if seen_dst[m.dst] > 1:
                out["duplicate destination"].append(rel)
            continue
        if not any(m.dst.startswith(r + os.sep) for r in roots):
            out["destination outside its category"].append(rel)
        if seen_dst[m.dst] > 1:
            out["duplicate destination"].append(rel)
        if DUP_SUFFIX.search(base):
            out["unparseable ' (N)' suffix"].append(rel)
        if EMPTY_SEGMENT.search(base):
            out["empty name segment"].append(rel)
        if RELEASE_LEFTOVER.search(base) and not base.startswith("."):
            out["release junk left in the name"].append(rel)
        if "  " in base:
            out["double space"].append(rel)
        ext = os.path.splitext(base)[1].lower()
        if ext in util.SUBTITLE_EXT and not SUB_LANG.search(base) \
                and os.path.splitext(base)[0] not in extras_stems:
            out["subtitle without a language code"].append(rel)
        if base != base.strip() or base.endswith("."):
            out["leading or trailing whitespace"].append(rel)

    seasons = collections.defaultdict(list)

    for m in moves:
        if os.path.splitext(m.dst)[1].lower() not in util.VIDEO_EXT:
            continue
        match = EPISODE.search(m.dst)
        if match:
            seasons[(match.group(1), match.group(2))].append(
                (int(match.group(5)), os.path.basename(m.dst)))
    for (show, season), items in seasons.items():
        # two files can legitimately share a number when they are different cuts,
        # which shows up as different episode titles; only flag real clashes
        by_number = collections.defaultdict(set)
        for number, base in items:
            by_number[number].add(base)
        numbers = [n for n, bases in by_number.items()
                   for _ in range(sum(1 for _n, _b in items if _n == n))
                   if len(bases) == 1]
        if len(set(numbers)) != len(numbers):
            dupes = sorted(n for n, c in collections.Counter(numbers).items() if c > 1)
            out["repeated episode number in one season"].append(
                f"{show[len(root) + 1:] if show.startswith(root) else show} "
                f"{season}: {dupes[:10]}")
    return out


def report(issues, limit: int = 8) -> int:
    if not issues:
        print("lint: clean")
        return 0
    total = sum(len(v) for v in issues.values())
    print(f"lint: {total} findings across {len(issues)} checks")
    for name in sorted(issues, key=lambda k: -len(issues[k])):
        rows = issues[name]
        print(f"\n  {name}: {len(rows)}")
        for row in rows[:limit]:
            print(f"    {row}")
        if len(rows) > limit:
            print(f"    ... {len(rows) - limit} more")
    return total
