"""Decide what each folder in a category actually is: a movie, a show, or a
grouping folder holding several titles. Anything uncertain gets a flag so a
reviewer can override it before the plan is applied."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import util
from .naming import TitleInfo
from .scan import Node, is_junk

LEADING_ORDINAL_RE = re.compile(
    r"(?i)^\s*(?:lesson|episode|ep|part|chapter|cap[ií]tulo|clase)?\s*"
    r"(\d{1,3})(?:\s*[-._]\s*|\s+)(?=\S)"
)
COURSE_LESSON_RE = re.compile(r"^\s*(\d{1,3})[-_](\d{1,3})\s*-\s*(.+)$")
MAX_DESCEND_DEPTH = 3


@dataclass
class Unit:
    kind: str                      # "movie" | "show"
    category: str
    src: Node                      # existing directory, or a loose file
    loose: bool                    # true when src is a single file with no folder
    info: TitleInfo
    flags: list = field(default_factory=list)
    seasons: dict = field(default_factory=dict)   # season_no -> [Node]
    notes: str = ""

    @property
    def src_rel(self) -> str:
        return self.src.rel


def season_number(name: str):
    if util.SPECIALS_DIR_RE.match(name.strip()):
        return 0
    m = util.SEASON_DIR_RE.match(name.strip())
    if m:
        return int(m.group(1))
    return None


EXTRAS_KEYWORDS = [
    ("behind the scenes", "Behind The Scenes"), ("behindthescenes", "Behind The Scenes"),
    ("deleted scene", "Deleted Scenes"), ("deletedscene", "Deleted Scenes"),
    ("outtake", "Deleted Scenes"), ("featurette", "Featurettes"),
    ("interview", "Interviews"), ("trailer", "Trailers"), ("short", "Shorts"),
    ("scene", "Scenes"), ("extra", "Other"), ("bonus", "Other"),
]


def _normalise_dir_label(name: str) -> str:
    """Undo the damage a previous pass did: `Featurettes (2015)`, `Foo - Extras`."""
    low = name.strip().lower()
    low = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", low)
    low = re.sub(r"\s*\([^)]*\)\s*$", "", low)
    low = re.sub(r"(?i)\s+(?:superseded|old|v\d+|backup)\b.*$", "", low)
    return low.strip(" -_.")


def is_extras_dir(name: str):
    low = _normalise_dir_label(name)
    direct = util.EXTRAS_DIRS.get(low)
    if direct:
        return direct
    tail = low.rsplit(" - ", 1)[-1].strip()
    direct = util.EXTRAS_DIRS.get(tail)
    if direct:
        return direct
    for needle, canon in EXTRAS_KEYWORDS:
        if low.startswith(needle) or tail.startswith(needle) or low.endswith(needle):
            return canon
    return None


SUBTITLE_DIR_NAMES = {"subs", "sub", "subtitles", "subtitulos", "subtitle",
                      "subtitulo", "legendas", "sottotitoli", "untertitel"}


def is_subtitle_dir(name: str) -> bool:
    return _normalise_dir_label(name) in SUBTITLE_DIR_NAMES


SUPPORT_DIR_NAMES = {"subs", "sub", "subtitles", "subtitulos", "subtitulos",
                     "scans", "soundtrack", "artwork", "covers", "cover",
                     "audio_ts", "bdmv", "certificate", "sample", "samples",
                     "proof", "screens", "screenshots"}


def is_support_dir(name: str) -> bool:
    """Directories holding real files that Plex should not scan as media."""
    if name.strip().upper() == "VIDEO_TS":
        return False
    low = _normalise_dir_label(name)
    if low in SUPPORT_DIR_NAMES:
        return True
    if is_extras_dir(name):
        return False
    return bool(re.match(r"(?i)^(subs?|subtitle|english audio|audio|scans?|soundtrack)\b", low))


def is_dvd_dir(node: Node) -> bool:
    """VIDEO_TS, and the "VIDEO TS" spelling an earlier pass left behind."""
    if not node.is_dir:
        return False
    normalised = re.sub(r"[\s_]+", "_", node.name.strip().upper())
    return normalised in {"VIDEO_TS", "AUDIO_TS"}


def has_dvd_structure(node: Node) -> bool:
    return any(is_dvd_dir(d) for d in node.dirs())


def only_dvd_content(node: Node) -> bool:
    """A folder whose video content is all inside a DVD structure is one movie."""
    if not has_dvd_structure(node):
        return False
    return not collect_videos(node, skip_special_dirs=True)


def collect_videos(node: Node, skip_special_dirs: bool = True):
    out = []
    for child in node.children:
        if child.is_dir:
            if child.is_hidden:
                continue
            if skip_special_dirs and (is_extras_dir(child.name) or is_support_dir(child.name)):
                continue
            out.extend(collect_videos(child, skip_special_dirs))
        elif child.is_video and not child.is_hidden:
            out.append(child)
    return out


def parse_episode(stem: str, show_title: str | None = None):
    """Return (season, episode, episode_end, title) from an episode filename."""
    ep = util.extract_episode(stem)
    if ep:
        season, num, num_end, (a, b) = ep
        rest = (stem[:a] + " " + stem[b:]).strip(" -._")
        return season, num, num_end, _clean_ep_title(rest, show_title)
    m = COURSE_LESSON_RE.match(stem)
    if m:
        return 1, int(m.group(2)), None, _split_course_title(m.group(3))
    m = LEADING_ORDINAL_RE.match(stem)
    if m:
        rest = stem[m.end():].strip(" -._")
        return 1, int(m.group(1)), None, _split_course_title(rest)
    return None


def _split_course_title(rest: str) -> str:
    """`Introduction - Bob Teaches X` keeps only the lesson name."""
    parts = [p.strip() for p in rest.split(" - ") if p.strip()]
    if len(parts) >= 2 and re.search(r"(?i)\b(teaches|masterclass|on)\b", parts[-1]):
        parts = parts[:-1]
    return " - ".join(parts).strip()


def _clean_ep_title(rest: str, show_title: str | None = None) -> str:
    rest = util.normalize_separators(rest)
    rest = util.strip_bracket_junk(rest)
    rest = util.strip_release_tail(rest)
    rest = re.sub(r"\s+", " ", rest).strip(" -._")
    if show_title:
        show_key = util.strip_accents_key(show_title)
        while show_key and util.strip_accents_key(rest).startswith(show_key):
            words = rest.split()
            trimmed = None
            for i in range(len(words), 0, -1):
                if util.strip_accents_key(" ".join(words[:i])) == show_key:
                    trimmed = " ".join(words[i:]).strip(" -._")
                    break
            if trimmed is None:
                break
            rest = trimmed
    rest = re.sub(r"\(\s*(?:19|20)\d{2}\s*\)", " ", rest)
    rest = re.sub(r"(?<![0-9])(?:19|20)\d{2}(?![0-9])", " ", rest)
    rest = re.sub(r"\s+", " ", rest).strip(" -._()[]{}")
    if not rest or rest.isdigit() or len(rest) < 2:
        return ""
    if not any(c.isalpha() for c in rest):
        return ""
    return util.smart_title(rest)


def numbered_sequence(videos):
    """True when every video ends in a number and they form a run from 0 or 1."""
    nums, bases = [], set()
    for v in videos:
        m = re.search(r"(?<![0-9])(\d{1,3})\s*$", v.stem)
        if not m:
            return None
        nums.append(int(m.group(1)))
        bases.add(util.strip_accents_key(v.stem[: m.start()]))
    if len(bases) != 1 or len(nums) < 2:
        return None
    ordered = sorted(nums)
    if len(set(nums)) != len(nums):
        return None
    if ordered[-1] - ordered[0] + 1 > len(ordered) * 1.6:
        return None
    return True


def title_keys(videos):
    keys = set()
    seq = numbered_sequence(videos)
    for v in videos:
        stem = v.stem
        if seq:
            stem = re.sub(r"(?<![0-9])\d{1,3}\s*$", "", stem)
        t = util.clean_title(stem)
        year = util.extract_year(t)
        if year:
            t = re.sub(rf"\(?{year}\)?", " ", t)
        t = re.sub(r"(?i)\s*-?\s*(?:part|pt|cd|disc|disk)\s*\d+\s*$", "", t)
        keys.add(util.strip_accents_key(t))
    keys.discard("")
    return keys


def episode_marked_ratio(videos):
    if not videos:
        return 0.0
    return sum(1 for v in videos if parse_episode(v.stem)) / len(videos)


def numbered_season_dirs(node: Node):
    return [d for d in node.dirs()
            if not is_extras_dir(d.name) and util.SEASON_DIR_RE.match(d.name.strip())]


def looks_like_show(node: Node):
    if numbered_season_dirs(node):
        return True
    vids = collect_videos(node)
    if not vids:
        return False
    if any(util.SXXEXX_RE.search(v.stem) or util.NXNN_RE.search(v.stem) for v in vids):
        return True
    if len(vids) < 2:
        return False
    if episode_marked_ratio(vids) >= 0.5:
        return True
    if numbered_sequence(vids) and len(vids) >= 4:
        return True
    return False


def looks_like_grouping(node: Node, depth: int):
    if depth >= MAX_DESCEND_DEPTH:
        return False
    subdirs = [d for d in node.dirs()
               if not is_extras_dir(d.name) and not is_support_dir(d.name)
               and not is_dvd_dir(d) and season_number(d.name) is None]
    direct = [v for v in node.videos() if not v.is_hidden]
    with_video = [d for d in subdirs if collect_videos(d)]
    if len(with_video) >= 2 and len(title_keys_dirs(with_video)) >= 2:
        return True
    if direct and len(title_keys(direct)) >= 2 and episode_marked_ratio(direct) < 0.5:
        return True
    return False


def title_keys_dirs(dirs):
    keys = set()
    for d in dirs:
        t = util.clean_title(d.name)
        y = util.extract_year(t)
        if y:
            t = re.sub(rf"\(?{y}\)?", " ", t)
        keys.add(util.strip_accents_key(t))
    keys.discard("")
    return keys


def suggest_title_from_episodes(videos):
    """MasterClass files carry the real course title as a trailing segment."""
    tails = []
    for v in videos:
        parts = [p.strip() for p in v.stem.split(" - ") if p.strip()]
        if len(parts) >= 3:
            tails.append(parts[-1])
    if len(tails) < 3 or len(tails) < len(videos) * 0.6:
        return None
    common = max(set(tails), key=tails.count)
    if tails.count(common) < len(tails) * 0.8 or len(common.split()) < 3:
        return None
    return common


def build_info(kind: str, raw_name: str, videos, category: str):
    ids = {}
    for prov in ("imdb", "tmdb", "tvdb", "anidb"):
        m = re.search(rf"(?i)\{{{prov}-([a-z0-9]+)\}}", raw_name)
        if m:
            ids[prov] = m.group(1)
    _, explicit = util.explicit_edition(raw_name)
    year = util.extract_year(raw_name)
    title = util.clean_title(raw_name)
    if year:
        title = re.sub(rf"\s*\(?{year}\)?\s*", " ", title).strip(" -._")
    title = re.sub(r"\{[^}]*\}", " ", title)
    title = util.SXXEXX_RE.sub(" ", title)
    title, editions = util.extract_editions(title)
    if explicit:
        editions = [explicit]
    title = re.sub(r"\s+", " ", title).strip(" -._")
    if year is None and videos:
        for v in videos:
            y = util.extract_year(v.stem)
            if y:
                year = y
                break
    return TitleInfo(kind=kind, title=title or raw_name, year=year, ids=ids,
                     editions=editions)


def group_seasons(node: Node, unit_videos):
    seasons = {}
    for v in unit_videos:
        season = None
        parent = v.parent
        while parent is not None and parent is not node:
            n = season_number(parent.name)
            if n is not None:
                season = n
                break
            if is_extras_dir(parent.name):
                season = 0
                break
            parent = parent.parent
        parsed = parse_episode(v.stem)
        if season is None and parsed and parsed[0] is not None:
            season = parsed[0]
        if season is None:
            season = 1
        seasons.setdefault(season, []).append(v)
    return seasons


def classify_category(category: str, root: Node):
    units = []
    for child in root.children:
        if child.is_hidden or is_junk(child):
            continue
        if child.is_dir:
            _classify_dir(category, child, units, depth=1)
        elif child.is_video:
            info = build_info("movie", child.stem, [child], category)
            units.append(Unit("movie", category, child, True, info,
                              flags=["loose-file"]))
    return units


def _classify_dir(category: str, node: Node, units, depth: int):
    if has_dvd_structure(node):
        info = build_info("movie", node.name, [], category)
        unit = Unit("movie", category, node, False, info, flags=["dvd-video_ts"],
                    notes="DVD folder structure, kept intact")
        if info.year is None:
            unit.flags.append("no-year")
        units.append(unit)
        return
    videos = collect_videos(node)
    if not videos:
        flags = ["dvd-video_ts"] if has_dvd_structure(node) else ["no-video"]
        note = ("DVD folder structure, kept intact" if has_dvd_structure(node)
                else "no video files found")
        units.append(Unit("movie", category, node, False,
                          build_info("movie", node.name, [], category),
                          flags=flags, notes=note))
        return
    if looks_like_show(node):
        info = build_info("show", node.name, videos, category)
        suggestion = suggest_title_from_episodes(videos)
        unit = Unit("show", category, node, False, info,
                    seasons=group_seasons(node, videos))
        if suggestion and util.strip_accents_key(suggestion) != util.strip_accents_key(info.title):
            unit.notes = f"episode files suggest the title {suggestion!r}"
            unit.flags.append("title-suggestion")
        if info.year is None:
            unit.flags.append("no-year")
        if episode_marked_ratio(videos) < 0.5 and not any(
                season_number(d.name) is not None for d in node.dirs()):
            unit.flags.append("inferred-episode-numbers")
        units.append(unit)
        return
    if looks_like_grouping(node, depth):
        for sub in node.dirs():
            if is_extras_dir(sub.name) or is_support_dir(sub.name) or sub.is_hidden:
                continue
            _classify_dir(category, sub, units, depth + 1)
        for v in node.videos():
            if v.is_hidden:
                continue
            info = build_info("movie", v.stem, [v], category)
            units.append(Unit("movie", category, v, True, info,
                              flags=["from-grouping"],
                              notes=f"was inside grouping folder {node.rel}"))
        leftovers = [f for f in node.files()
                     if not f.is_video and not f.is_hidden and is_junk(f)]
        if leftovers:
            units.append(Unit("movie", category, node, False,
                              build_info("movie", node.name, [], category),
                              flags=["grouping-leftovers", "hidden"],
                              notes="junk left at the level of a grouping folder"))
        return
    info = build_info("movie", node.name, videos, category)
    if len(videos) == 1:
        richer = build_info("movie", videos[0].stem, videos, category)
        folder_key = util.strip_accents_key(info.title)
        file_key = util.strip_accents_key(richer.title)
        if folder_key and file_key.startswith(folder_key) and len(file_key) > len(folder_key):
            richer.ids = richer.ids or info.ids
            richer.year = richer.year or info.year
            info = richer
    unit = Unit("movie", category, node, False, info)
    if info.year is None:
        unit.flags.append("no-year")
    if len(videos) > 1 and len(title_keys(videos)) > 1:
        unit.flags.append("multiple-titles-in-movie-folder")
    units.append(unit)
