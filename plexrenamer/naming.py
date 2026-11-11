"""Plex naming rules.

Movie library
    Category/Title (Year) {tmdb-NNN}/Title (Year) {edition-Name} - part1.ext
TV library
    Category/Show (Year) {tvdb-NNN}/Season 01/Show (Year) - S01E01 - Title.ext
Extras
    <title folder>/<Extras Folder>/Name.ext   or   Name-<suffix>.ext
Subtitles
    <same stem as the video>.<lang>[.forced][.sdh][.<custom>].srt
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import util

ID_ORDER = ("imdb", "tmdb", "tvdb", "anidb")


@dataclass
class TitleInfo:
    kind: str                      # "movie" | "show"
    title: str
    year: int | None = None
    ids: dict = field(default_factory=dict)
    editions: list = field(default_factory=list)

    def base(self) -> str:
        """Title (Year) - the stem shared by folder and files."""
        name = util.sanitize(self.title)
        if self.year:
            name = f"{name} ({self.year})"
        return name

    def id_suffix(self) -> str:
        """Plex documents a single matching hint, so emit exactly one."""
        for provider in ID_ORDER:
            value = self.ids.get(provider)
            if value:
                return f" {{{provider}-{value}}}"
        return ""

    def folder(self) -> str:
        """Plex: Title (Year) {imdb-tt..} {edition-Name} for the per-title folder."""
        return util.sanitize(self.base() + self.id_suffix()
                             + edition_suffix(self.editions))


def edition_suffix(editions) -> str:
    if not editions:
        return ""
    return " {edition-" + util.sanitize(", ".join(editions)).replace("{", "").replace("}", "") + "}"


# Plex's docs write the marker lowercase (s01e01); every mainstream tool already
# uses uppercase. Both match, the token is case-insensitive.
EPISODE_CASE = "upper"


def movie_file(info: TitleInfo, ext: str, part: int | None = None,
               version: str | None = None) -> str:
    """Plex: /Movies/Title (Year) {imdb-tt..}/Title (Year) {imdb-tt..}.ext"""
    name = info.base() + info.id_suffix()
    if version:
        name += f" - {util.sanitize(version)}"
    if part:
        name += f" - part{part}"
    name += edition_suffix(info.editions)
    return util.sanitize(name) + ext.lower()


def season_folder(season: int) -> str:
    return "Specials" if season == 0 else f"Season {season:02d}"


def episode_marker(season: int, episode: int, episode_end: int | None = None) -> str:
    s, e = ("S", "E") if EPISODE_CASE == "upper" else ("s", "e")
    marker = f"{s}{season:02d}{e}{episode:02d}"
    if episode_end and episode_end != episode:
        marker += f"-{e}{episode_end:02d}"
    return marker


def episode_stem(info: TitleInfo, season: int, episode: int,
                 episode_end: int | None = None, ep_title: str | None = None) -> str:
    stem = f"{info.base()} - {episode_marker(season, episode, episode_end)}"
    if False:
        pass
    if ep_title:
        clean = util.sanitize(ep_title).strip(" -")
        if clean:
            stem += f" - {clean}"
    return util.sanitize(stem)


def episode_file(info: TitleInfo, season: int, episode: int, ext: str,
                 episode_end: int | None = None, ep_title: str | None = None) -> str:
    return episode_stem(info, season, episode, episode_end, ep_title) + ext.lower()


def subtitle_file(video_stem: str, lang: str | None, forced: bool = False,
                  sdh: bool = False, custom: str | None = None,
                  ext: str = ".srt") -> str:
    name = video_stem
    if custom:
        clean = util.sanitize(custom).strip()
        if clean:
            name += f".{clean}"
    if lang:
        name += f".{lang}"
    if forced:
        name += ".forced"
    if sdh:
        name += ".sdh"
    return util.sanitize(name) + ext.lower()


def extras_file(folder_kind: str, label: str, ext: str) -> str:
    """A file inside a recognised extras folder keeps a descriptive name."""
    return util.sanitize(label) + ext.lower()


def extras_flat_file(label: str, folder_kind: str, ext: str) -> str:
    """An extra stored beside the movie uses the suffix form."""
    suffix = util.EXTRA_SUFFIX.get(folder_kind, "other")
    return util.sanitize(f"{label}-{suffix}") + ext.lower()


LOCAL_ASSET_MAP = {
    "poster": "poster", "folder": "poster", "cover": "poster", "movie": "poster",
    "default": "poster", "show": "poster",
    "background": "background", "fanart": "background", "backdrop": "background",
    "art": "background",
    "banner": "banner", "theme": "theme", "logo": "clearlogo",
}


def local_asset_name(stem: str, ext: str) -> str | None:
    """Map an artwork filename to the canonical Plex local-asset name."""
    canon = LOCAL_ASSET_MAP.get(stem.strip().lower())
    return (canon + ext.lower()) if canon else None
