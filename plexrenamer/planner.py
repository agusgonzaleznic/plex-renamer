"""Turn classified units into a concrete list of file moves.

The planner only ever emits file-level moves. Directory creation and the
pruning of directories we emptied are derived by the executor, which keeps
the journal a flat, reversible sequence.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace

from . import naming, util
from .classify import (Unit, is_dvd_dir, is_extras_dir, is_subtitle_dir,
                       is_support_dir, parse_episode, season_number)
from .scan import Node, is_junk
from .sidecars import (PART_RE, _dedupe, _free_path, _hidden, _label,
                       _sub_flavour, _unique_subtitle)

@dataclass
class Move:
    src: str
    dst: str
    is_dir: bool
    size: int
    inode: int
    category: str
    unit: str
    reason: str


@dataclass
class UnitPlan:
    unit: Unit
    dest_dir: str
    moves: list = field(default_factory=list)
    prunes: list = field(default_factory=list)
    flags: list = field(default_factory=list)
    notes: list = field(default_factory=list)


class Planner:
    def __init__(self, root: str):
        self.root = root

    # ---------------------------------------------------------------- entry
    def plan_unit(self, unit: Unit) -> UnitPlan:
        dest_dir = os.path.join(self.root, unit.category, unit.info.folder())
        plan = UnitPlan(unit=unit, dest_dir=dest_dir, flags=list(unit.flags))
        if "no-video" in unit.flags and unit.src.is_dir and not unit.src.children:
            if unit.src.path == dest_dir:
                plan.notes.append("empty directory, already correctly named")
            elif os.path.isdir(dest_dir):
                # the contents already moved on an earlier run, so this is a
                # leftover shell rather than a folder waiting to be renamed
                plan.prunes.append(unit.src.path)
                plan.notes.append("emptied by an earlier run, removed")
            else:
                plan.moves.append(Move(
                    src=unit.src.path, dst=dest_dir, is_dir=True, size=0,
                    inode=unit.src.inode, category=unit.category,
                    unit=unit.src_rel, reason="empty folder renamed"))
                plan.notes.append("empty directory, only the folder name changes")
            return plan
        if "hidden" in unit.flags:
            self._plan_hidden_unit(unit, plan)
        elif unit.loose:
            self._plan_loose(unit, plan)
        elif unit.kind == "show":
            self._plan_show(unit, plan)
        else:
            self._plan_movie(unit, plan)
        return plan

    # --------------------------------------------------------------- movies
    def _plan_hidden_unit(self, unit: Unit, plan: UnitPlan):
        """Keep every file, but under a dot folder so the scanner walks past it.

        The dot goes on the first path component below the category, so a unit
        nested inside a grouping folder keeps its shape instead of being
        scattered across the category root.
        """
        category_root = os.path.join(self.root, unit.category)
        inside = os.path.relpath(unit.src.path, category_root).split(os.sep)
        base = os.path.join(category_root, _hidden(inside[0]), *inside[1:])
        plan.dest_dir = base
        if unit.loose:
            self._add(plan, unit.src, base, "hidden from the scanner by decision")
            return
        for node in unit.src.walk():
            if node.is_dir or node is unit.src:
                continue
            rest = os.path.relpath(node.path, unit.src.path)
            self._add(plan, node, os.path.join(base, rest),
                      "hidden from the scanner by decision")

    def _plan_loose(self, unit: Unit, plan: UnitPlan):
        node = unit.src
        dst = os.path.join(plan.dest_dir, naming.movie_file(unit.info, node.ext))
        self._add(plan, node, dst, "loose file moved into its own movie folder")

    def _plan_movie(self, unit: Unit, plan: UnitPlan):
        used = {}
        primaries = self._primary_videos(unit.src)
        groups = self._edition_groups(unit, primaries)
        stem_for = {}
        default_stem = None
        primary_dest = None
        for edition, nodes in groups:
            info = unit.info if edition is None else replace(
                unit.info, editions=[edition])
            dest = plan.dest_dir if edition is None else os.path.join(
                os.path.dirname(plan.dest_dir), info.folder())
            if primary_dest is None:
                primary_dest = dest
            local = used if dest == primary_dest else {}
            for node, part in self._assign_parts(nodes):
                name = _dedupe(local, naming.movie_file(info, node.ext, part=part))
                stem_for[node] = os.path.splitext(name)[0]
                if default_stem is None:
                    default_stem = stem_for[node]
                self._add(plan, node, os.path.join(dest, name),
                          "movie file" + (f" part {part}" if part else ""))
        if len(groups) > 1:
            plan.flags.append("split-into-edition-folders")
        elif len(primaries) > 1 and not any(
                PART_RE.search(n.stem) for n in primaries):
            plan.flags.append("multiple-primary-videos")
        if default_stem is None:
            default_stem = unit.info.base()
        if primary_dest:
            plan.dest_dir = primary_dest
        src_stem = primaries[0].stem if primaries else None
        self._plan_side_files(unit.src, plan, plan.dest_dir, default_stem,
                              stem_for, used, season=None, src_stem=src_stem)

    def _edition_groups(self, unit: Unit, primaries):
        """Plex wants one folder per edition, so split the unit when needed."""
        buckets = {}
        for node in primaries:
            _, edition = util.explicit_edition(node.stem)
            buckets.setdefault(edition, []).append(node)
        if len(buckets) <= 1:
            only = next(iter(buckets), None)
            if only and not unit.info.editions:
                return [(only, buckets[only])]
            return [(None, primaries)]
        return sorted(buckets.items(), key=lambda kv: (kv[0] or ""))

    def _primary_videos(self, node: Node):
        out = []
        for child in node.children:
            if child.is_hidden:
                continue
            if child.is_dir:
                if is_dvd_dir(child) or child.name.strip().upper() == "VIDEO TS":
                    continue
                if is_extras_dir(child.name) or is_support_dir(child.name):
                    continue
                out.extend(self._primary_videos(child))
            elif child.is_video:
                out.append(child)
        return sorted(out, key=lambda n: (-n.size, n.name)) if len(out) > 1 else out

    def _assign_parts(self, videos):
        if len(videos) <= 1:
            return [(v, None) for v in videos]
        explicit = [(v, PART_RE.search(v.stem)) for v in videos]
        if all(m for _, m in explicit):
            return sorted([(v, int(m.group(1))) for v, m in explicit],
                          key=lambda t: t[1])
        if any(m for _, m in explicit):
            # some already carry a part token and some do not: these are separate
            # versions, not one stack, so keep the existing tokens and leave the
            # rest unstacked
            return [(v, int(m.group(1)) if m else None) for v, m in explicit]
        if len({v.ext for v in videos}) > 1:
            # a stack must be one container format throughout
            return [(v, None) for v in videos]
        ordered = sorted(videos, key=lambda n: n.name)
        return [(v, i + 1) for i, v in enumerate(ordered)]

    # ---------------------------------------------------------------- shows
    def _plan_show(self, unit: Unit, plan: UnitPlan):
        counters = {}
        stem_for = {}
        dir_for = {}
        used_by_dir = {}
        for season in sorted(unit.seasons):
            season_dir = os.path.join(plan.dest_dir, naming.season_folder(season))
            used = used_by_dir.setdefault(season_dir, {})
            videos = unit.seasons[season]
            renumber = self._numbers_are_unreliable(videos, unit.info.title)
            if renumber and "renumbered-sequentially" not in plan.flags:
                plan.flags.append("renumbered-sequentially")
            ordered = (sorted(videos, key=lambda n: n.name) if renumber
                       else self._ordered(videos, unit.info.title))
            for position, node in enumerate(ordered, start=1):
                num, num_end, ep_title = self._episode_number(
                    node, season, counters, unit.info.title)
                if renumber:
                    num, num_end = position, None
                name = _dedupe(used, naming.episode_file(
                    unit.info, season, num, node.ext, num_end, ep_title))
                stem_for[node] = os.path.splitext(name)[0]
                dir_for[node] = season_dir
                self._add(plan, node, os.path.join(season_dir, name),
                          f"S{season:02d}E{num:02d}")
        self._plan_show_side_files(unit.src, unit, plan, stem_for, dir_for,
                                   used_by_dir)
        self._plan_show_extras(unit, plan)

    def _numbers_are_unreliable(self, videos, title):
        """A shared prefix like `150 - ` is a course id, not an episode number."""
        if len(videos) < 3:
            return False
        parsed = [parse_episode(v.stem, title) for v in videos]
        known = [p[1] for p in parsed if p]
        if not known:
            return False
        return len(set(known)) < max(2, len(videos) * 0.6)

    def _ordered(self, videos, title=None):
        def key(n):
            parsed = parse_episode(n.stem, title)
            return (0, parsed[1]) if parsed else (1, n.name)
        return sorted(videos, key=key)

    def _episode_number(self, node: Node, season: int, counters: dict, title=None):
        parsed = parse_episode(node.stem, title)
        if parsed:
            _, num, num_end, title = parsed
            counters[season] = max(counters.get(season, 0), num)
            return num, num_end, title
        m = re.search(r"(?<![0-9])(\d{1,3})\s*$", node.stem)
        if m:
            num = int(m.group(1))
            counters[season] = max(counters.get(season, 0), num)
            return num, None, ""
        counters[season] = counters.get(season, 0) + 1
        return counters[season], None, ""

    def _plan_show_side_files(self, node: Node, unit: Unit, plan: UnitPlan,
                              stem_for, dir_for, used_by_dir):
        """Walk the show tree once, routing each sidecar to its episode."""
        for child in node.children:
            if child.is_hidden:
                continue
            if child.is_dir:
                if is_extras_dir(child.name) and not self._only_subtitles(child):
                    continue
                if is_support_dir(child.name) and not is_subtitle_dir(child.name):
                    continue
                self._plan_show_side_files(child, unit, plan, stem_for, dir_for,
                                           used_by_dir)
                continue
            if child.is_video:
                continue
            match = self._match_episode(child, stem_for, dir_for)
            if is_junk(child):
                self._add(plan, child,
                          _free_path(os.path.join(plan.dest_dir, _hidden(child.name))),
                          "junk hidden from the scanner")
                continue
            if child.is_subtitle and not match:
                rest = os.path.relpath(child.path, unit.src.path)
                self._add(plan, child,
                          os.path.join(plan.dest_dir, ".Unmatched Subs", rest),
                          "subtitle with no matching episode, hidden")
                continue
            if child.is_subtitle and match:
                src_stem, dest_stem, dest_dir = match
                lang, forced, sdh, leftover = _sub_flavour(child.stem, src_stem)
                if lang is None:
                    lang = "en"
                used = used_by_dir.setdefault(dest_dir, {})
                name = _unique_subtitle(used, dest_stem, lang, forced, sdh,
                                        leftover, child.ext, child.name)
                self._add(plan, child, os.path.join(dest_dir, name),
                          "episode subtitle")
                continue
            if child.is_image:
                asset = naming.local_asset_name(child.stem, child.ext)
                target = os.path.join(plan.dest_dir, asset) if asset else \
                    os.path.join(plan.dest_dir, _hidden(child.name))
                self._add(plan, child, target,
                          "local artwork" if asset else
                          "unrecognised image hidden from the scanner")
                continue
            self._add(plan, child, os.path.join(plan.dest_dir, child.name),
                      "companion file kept alongside")

    @staticmethod
    def _only_subtitles(node: Node) -> bool:
        files = [n for n in node.walk() if not n.is_dir and not n.is_hidden]
        return bool(files) and all(f.is_subtitle for f in files)

    def _match_episode(self, child: Node, stem_for, dir_for):
        """Match a sidecar to its episode by stem, then by episode marker, then
        by the marker on any enclosing folder (release tools nest per-episode
        subtitle folders inside a Subs directory)."""
        best, best_len = None, -1
        ckey = util.strip_accents_key(child.stem)
        for node, stem in stem_for.items():
            nkey = util.strip_accents_key(node.stem)
            if not nkey or not ckey.startswith(nkey):
                continue
            if len(nkey) > best_len:
                best, best_len = (node.stem, stem, dir_for[node]), len(nkey)
        if best:
            return best
        parsed = parse_episode(child.stem)
        if parsed:
            for node, stem in stem_for.items():
                other = parse_episode(node.stem)
                if other and other[0] == parsed[0] and other[1] == parsed[1]:
                    return node.stem, stem, dir_for[node]
        ancestor = child.parent
        while ancestor is not None:
            marker = parse_episode(ancestor.name)
            if marker:
                for node, stem in stem_for.items():
                    other = parse_episode(node.stem)
                    if other and other[0] == marker[0] and other[1] == marker[1]:
                        return node.stem, stem, dir_for[node]
                break
            ancestor = ancestor.parent
        siblings = [(n.stem, s, dir_for[n]) for n, s in stem_for.items()
                    if n.parent is child.parent]
        if len(siblings) == 1:
            return siblings[0]
        if len(stem_for) == 1:
            # a one-episode show: any loose sidecar can only belong to that episode
            node, stem = next(iter(stem_for.items()))
            return node.stem, stem, dir_for[node]
        return None

    def _plan_show_extras(self, unit: Unit, plan: UnitPlan):
        used = {}
        for child in unit.src.children:
            if not child.is_dir or child.is_hidden:
                continue
            season = season_number(child.name)
            if season is not None:
                # extras belonging to one season live under that season folder
                season_dir = os.path.join(plan.dest_dir,
                                          naming.season_folder(season))
                for grandchild in child.children:
                    if not grandchild.is_dir or grandchild.is_hidden:
                        continue
                    nested = is_extras_dir(grandchild.name)
                    if nested:
                        self._plan_extras_dir(
                            grandchild, plan, os.path.join(season_dir, nested),
                            nested, {})
                    elif is_support_dir(grandchild.name) \
                            and not is_subtitle_dir(grandchild.name):
                        self._plan_hidden_dir(grandchild, plan, season_dir)
                continue
            canon = is_extras_dir(child.name)
            if canon and self._only_subtitles(child):
                # an extras folder holding nothing but subtitles is a sidecar
                # folder, so the files belong next to their episodes
                continue
            if canon:
                self._plan_extras_dir(child, plan, os.path.join(plan.dest_dir, canon),
                                      canon, used)
            elif (is_support_dir(child.name) and not is_subtitle_dir(child.name)
                  and season_number(child.name) is None):
                self._plan_hidden_dir(child, plan, plan.dest_dir)

    # ----------------------------------------------------------- side files
    def _plan_side_files(self, node: Node, plan: UnitPlan, dest_dir: str,
                         default_stem, stem_for, used, season, src_stem=None):
        for child in node.children:
            if child.is_hidden:
                continue
            if child.is_dir:
                if season is not None and season_number(child.name) is not None:
                    self._plan_side_files(child, plan, dest_dir, default_stem,
                                          stem_for, used, season, src_stem)
                    continue
                if season is not None:
                    continue
                if is_dvd_dir(child) or child.name.strip().upper() == "VIDEO TS":
                    self._plan_dvd(child, plan, dest_dir)
                    continue
                canon = is_extras_dir(child.name)
                if canon:
                    self._plan_extras_dir(child, plan, os.path.join(dest_dir, canon),
                                          canon, {})
                elif is_support_dir(child.name):
                    self._plan_support_dir(child, plan, dest_dir, default_stem,
                                           stem_for, used, src_stem)
                else:
                    self._plan_side_files(child, plan, dest_dir, default_stem,
                                          stem_for, used, season, src_stem)
                continue
            if child.is_video:
                continue
            self._plan_companion(child, plan, dest_dir, default_stem, stem_for,
                                 used, src_stem)

    def _plan_companion(self, child: Node, plan: UnitPlan, dest_dir: str,
                        default_stem, stem_for, used, src_stem=None):
        if is_junk(child):
            self._add(plan, child,
                      _free_path(os.path.join(dest_dir, _hidden(child.name))),
                      "junk hidden from the scanner")
            return
        if child.is_subtitle:
            match = self._stem_for_sibling(child, stem_for, default_stem)
            if match is None:
                return
            src_stem, stem = match
            lang, forced, sdh, leftover = _sub_flavour(child.stem, src_stem or stem)
            if lang is None:
                # Plex ignores a sidecar with no language code, so always emit one
                lang = "en"
            name = _unique_subtitle(used, stem, lang, forced, sdh, leftover,
                                    child.ext, child.name)
            self._add(plan, child, os.path.join(dest_dir, name), "subtitle")
            return
        if child.is_image:
            asset = naming.local_asset_name(child.stem, child.ext)
            if asset is None:
                image_key = util.strip_accents_key(child.stem)
                for candidate in (src_stem, default_stem):
                    if candidate and image_key == util.strip_accents_key(candidate):
                        asset = "poster" + child.ext
                        break
            if asset:
                self._add(plan, child, os.path.join(dest_dir, _dedupe(used, asset)),
                          "local artwork")
            else:
                self._add(plan, child, os.path.join(dest_dir, _hidden(child.name)),
                          "unrecognised image hidden from the scanner")
            return
        self._add(plan, child, os.path.join(dest_dir, _dedupe(used, child.name)),
                  "companion file kept alongside")

    def _stem_for_sibling(self, child: Node, stem_for, default_stem):
        """Return (source video stem, destination stem) for a sidecar file."""
        best, best_len = None, -1
        ckey = util.strip_accents_key(child.stem)
        for node, stem in stem_for.items():
            if node.parent is not child.parent:
                continue
            nkey = util.strip_accents_key(node.stem)
            if ckey.startswith(nkey) and len(nkey) > best_len:
                best, best_len = (node.stem, stem), len(nkey)
        if best:
            return best
        parsed = parse_episode(child.stem)
        if parsed:
            for node, stem in stem_for.items():
                other = parse_episode(node.stem)
                if other and other[0] == parsed[0] and other[1] == parsed[1]:
                    return node.stem, stem
        siblings = [(n.stem, s) for n, s in stem_for.items()
                    if n.parent is child.parent]
        if len(siblings) == 1:
            return siblings[0]
        if default_stem is None:
            return None
        return None, default_stem

    def _plan_support_dir(self, node: Node, plan: UnitPlan, dest_dir: str,
                          default_stem, stem_for, used, src_stem=None):
        """Subs/ and friends: pull subtitles up beside the video, hide the rest."""
        for child in node.children:
            if child.is_hidden:
                continue
            if child.is_dir:
                self._plan_support_dir(child, plan, dest_dir, default_stem,
                                       stem_for, used, src_stem)
                continue
            if child.is_subtitle and default_stem:
                lang, forced, sdh, leftover = _sub_flavour(child.stem,
                                                           src_stem or default_stem)
                if lang is None:
                    lang = "en"
                name = _unique_subtitle(used, default_stem, lang, forced, sdh,
                                        leftover, child.ext, child.name)
                self._add(plan, child, os.path.join(dest_dir, name),
                          "subtitle lifted out of a Subs folder")
            elif is_junk(child):
                self._add(plan, child,
                          _free_path(os.path.join(dest_dir, _hidden(child.name))),
                          "junk hidden from the scanner")
            else:
                self._add(plan, child,
                          os.path.join(dest_dir, _hidden(node.name), child.name),
                          "support folder hidden from the scanner")

    def _plan_hidden_dir(self, node: Node, plan: UnitPlan, dest_dir: str,
                         prefix: str | None = None):
        base = prefix or os.path.join(dest_dir, _hidden(node.name))
        for child in node.children:
            if child.is_dir:
                self._plan_hidden_dir(child, plan, dest_dir,
                                      os.path.join(base, child.name))
            else:
                self._add(plan, child, os.path.join(base, child.name),
                          "support folder hidden from the scanner")

    def _plan_dvd(self, node: Node, plan: UnitPlan, dest_dir: str):
        for child in node.children:
            if child.is_dir:
                self._plan_dvd(child, plan, os.path.join(dest_dir, "VIDEO_TS"))
            else:
                self._add(plan, child, os.path.join(dest_dir, "VIDEO_TS", child.name),
                          "DVD structure preserved")

    def _plan_extras_dir(self, node: Node, plan: UnitPlan, dest_dir: str,
                         canon: str, used):
        videos = {util.strip_accents_key(c.stem): _label(c.stem)
                  for c in node.children if c.is_video and not c.is_hidden}
        for child in node.children:
            if child.is_hidden:
                continue
            if child.is_dir:
                nested = is_extras_dir(child.name) or canon
                target = os.path.join(os.path.dirname(dest_dir), nested)
                self._plan_extras_dir(child, plan, target, nested, used)
                continue
            if is_junk(child):
                self._add(plan, child,
                          _free_path(os.path.join(dest_dir, _hidden(child.name))),
                          "junk hidden from the scanner")
                continue
            if child.is_subtitle:
                key = util.strip_accents_key(child.stem)
                owner = next((label for vkey, label in videos.items()
                              if key.startswith(vkey)), None)
                if owner:
                    src_stem = next(c.stem for c in node.children
                                    if c.is_video and _label(c.stem) == owner)
                    lang, forced, sdh, leftover = _sub_flavour(child.stem, src_stem)
                    name = _unique_subtitle(used, owner, lang or "en", forced, sdh,
                                            leftover, child.ext, child.name)
                    self._add(plan, child, os.path.join(dest_dir, name),
                              f"extra subtitle ({canon})")
                    continue
            name = _dedupe(used, util.sanitize(_label(child.stem)) + child.ext.lower())
            self._add(plan, child, os.path.join(dest_dir, name), f"extra ({canon})")

    # ------------------------------------------------------------- plumbing
    def _add(self, plan: UnitPlan, node: Node, dst: str, reason: str):
        if node.path == dst:
            return
        plan.moves.append(Move(src=node.path, dst=dst, is_dir=node.is_dir,
                               size=node.size, inode=node.inode,
                               category=plan.unit.category,
                               unit=plan.unit.src_rel, reason=reason))
