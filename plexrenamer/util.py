"""Filename analysis helpers: token stripping, year/episode extraction, sanitising."""

from __future__ import annotations

import re
import unicodedata

VIDEO_EXT = {
    ".mkv", ".mp4", ".avi", ".m4v", ".mpg", ".mpeg", ".mov", ".wmv", ".flv",
    ".webm", ".ts", ".m2ts", ".iso", ".vob", ".ogm", ".rm", ".divx", ".mk3d",
}
SUBTITLE_EXT = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".smi", ".psb"}
AUDIO_EXT = {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aac", ".ac3", ".dts"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tbn", ".webp", ".gif", ".bmp"}
DOC_EXT = {".pdf", ".epub", ".txt", ".doc", ".docx", ".rtf", ".jsonl", ".csv"}
SIDECAR_EXT = {".nfo", ".xml", ".m3u", ".m3u8", ".log", ".md5", ".sfv", ".ifo", ".bup"}

# Tokens that mark the end of a title in a release-scene filename.
STOP_TOKENS = {
    # resolution / quality
    "480p", "540p", "576p", "720p", "1080p", "1440p", "2160p", "4320p", "4k", "8k",
    "uhd", "fhd", "hd", "sd", "hq", "hdrip", "sdr",
    # source
    "bluray", "blu-ray", "bdrip", "brrip", "bdremux", "remux", "webrip", "web-dl",
    "webdl", "web", "hdtv", "pdtv", "dvdrip", "dvdscr", "dvd", "dvd5", "dvd9",
    "camrip", "cam", "telesync", "telecine", "vhsrip", "vodrip", "tvrip", "hdcam",
    # streaming services
    "amzn", "nf", "dsnp", "hmax", "atvp", "itunes", "stan", "crav", "pcok", "hulu",
    "pmtp", "roku",
    # codecs
    "x264", "x265", "h264", "h265", "h.264", "h.265", "hevc", "avc", "xvid", "divx",
    "vp9", "av1", "10bit", "8bit", "12bit", "hi10p", "hi10", "10bits",
    # audio
    "aac", "aac2", "ac3", "eac3", "dts", "dtshd", "truehd", "atmos", "ddp", "ddp5",
    "dd5", "dd", "flac", "opus", "mp3", "lpcm", "pcm", "2ch", "6ch", "8ch",
    "aac5", "ddp2", "dd2", "ac3d", "md",
    # hdr
    "hdr", "hdr10", "hdr10+", "dv", "dovi", "hlg",
    # release flags
    "repack", "proper", "internal", "limited", "dubbed", "subbed", "multi", "dual",
    "readnfo", "rerip", "nfofix", "complete", "batch",
    # language / subtitle markers used by scene groups
    "eng", "ita", "spa", "fre", "ger", "jpn", "kor", "chi", "rus", "por", "dut",
    "sub", "subs", "vostfr", "multisub", "esub", "msubs",
}

# Release group / site markers. Split in two: names that are never a real word
# match anywhere, names that could be part of a title (FLUX, EDITH, MINX, CAKES)
# only match in a release-tail position, immediately after "-", "[" or ".".
SITE_MARKERS = re.compile(
    r"(?ix)\b(?:yts|yify|rarbg|eztv|ettv|edu4u|medbay|theblackking|galaxyrg|"
    r"publichd|shaanig|successfulcrab|megusta|nogrp|qxr|tigole|mzabi|ytsproxies|"
    r"yifystatus)\b"
)
GROUP_MARKERS_TAIL = re.compile(
    r"(?ix)(?<=[-\[.])(?:evo|sparks|fgt|ntb|tbk|flux|ntg|edith|kogi|rcvr|minx|"
    r"cakes|axxo|d3g|ion10|utr|playweb|psa|ar)\b"
)


def _release_marker(name: str):
    hits = [m for m in (SITE_MARKERS.search(name), GROUP_MARKERS_TAIL.search(name))
            if m]
    return min(hits, key=lambda m: m.start()) if hits else None


# Bracketed junk blocks that scene tools leave behind, e.g. "[YTS.MX]", "[1080p]".
BRACKET_JUNK = re.compile(
    r"(?ix)\[(?:[^\[\]]*?(?:yts|yify|rarbg|1080p|2160p|720p|480p|4k|web|webrip|"
    r"bluray|bdrip|x264|x265|hevc|10bit|8bit|hdr|aac|ac3|eac3|dts|atmos|"
    r"\d\.\d)[^\[\]]*?)\]"
)

EDITION_PATTERNS = [
    (r"(?i)\bdirector'?s?\s+cut\b", "Director's Cut"),
    (r"(?i)\bfinal\s+cut\b", "Final Cut"),
    (r"(?i)\bextended\s+unrated\b", "Extended Unrated"),
    (r"(?i)\bextended(?:\s+(?:edition|cut|version))?\b", "Extended"),
    (r"(?i)\bunrated\b", "Unrated"),
    (r"(?i)\buncut\b", "Uncut"),
    (r"(?i)\btheatrical(?:\s+cut)?\b", "Theatrical"),
    (r"(?i)\bremaster(?:ed)?\b", "Remastered"),
    (r"(?i)\bremasterizado\b", "Remasterizado"),
    (r"(?i)\bspecial\s+edition\b", "Special Edition"),
    (r"(?i)\bultimate\s+edition\b", "Ultimate Edition"),
    (r"(?i)\banniversary\s+edition\b", "Anniversary Edition"),
    (r"(?i)\bimax\b", "IMAX"),
    (r"(?i)\bredux\b", "Redux"),
    (r"(?i)\bcriterion\b", "Criterion"),
]

YEAR_RE = re.compile(r"(?<![0-9])(19[0-9]{2}|20[0-9]{2})(?![0-9])")
YEAR_PAREN_RE = re.compile(r"\((19[0-9]{2}|20[0-9]{2})\)")

SXXEXX_RE = re.compile(
    r"(?i)(?<![a-z0-9])s\s?(\d{1,3})\s?[._ -]?\s?e\s?(\d{1,3})"
    r"(?:\s?[-_ ]?\s?e\s?(\d{1,3}))?(?![0-9])"
)
NXNN_RE = re.compile(r"(?<![0-9a-zA-Z])(\d{1,2})x(\d{2,3})(?![0-9])")
SEASON_WORD_RE = re.compile(r"(?i)\bseason\s*(\d{1,3})\b")
EPISODE_WORD_RE = re.compile(r"(?i)\bepisode\s*(\d{1,4})\b")
SEASON_DIR_RE = re.compile(r"(?i)^(?:season|series|s|temporada|staffel|saison)[\s._-]*(\d{1,3})\b")
SPECIALS_DIR_RE = re.compile(r"(?i)^(specials?|season\s*0+|ovas?)$")

# Plex extras folder names (canonical spelling).
EXTRAS_DIRS = {
    "behind the scenes": "Behind The Scenes",
    "behindthescenes": "Behind The Scenes",
    "deleted scenes": "Deleted Scenes",
    "deletedscenes": "Deleted Scenes",
    "featurettes": "Featurettes",
    "featurette": "Featurettes",
    "interviews": "Interviews",
    "interview": "Interviews",
    "scenes": "Scenes",
    "shorts": "Shorts",
    "short": "Shorts",
    "trailers": "Trailers",
    "trailer": "Trailers",
    "other": "Other",
    "extras": "Other",
    "bonus": "Other",
    "specials": "Other",
}
EXTRA_SUFFIX = {
    "Behind The Scenes": "behindthescenes",
    "Deleted Scenes": "deleted",
    "Featurettes": "featurette",
    "Interviews": "interview",
    "Scenes": "scene",
    "Shorts": "short",
    "Trailers": "trailer",
    "Other": "other",
}

LANG_NAMES = {
    "english": "en", "eng": "en", "en": "en", "inglés": "en", "ingles": "en",
    "spanish": "es", "spa": "es", "esp": "es", "es": "es", "castellano": "es",
    "european": "es", "latin american": "es", "latino": "es",
    "portuguese": "pt", "por": "pt", "pt": "pt", "brazilian": "pt", "bra": "pt",
    "french": "fr", "fre": "fr", "fra": "fr", "fr": "fr", "français": "fr",
    "german": "de", "ger": "de", "deu": "de", "de": "de", "deutsch": "de",
    "italian": "it", "ita": "it", "it": "it", "italiano": "it",
    "japanese": "ja", "jpn": "ja", "jap": "ja", "ja": "ja",
    "korean": "ko", "kor": "ko", "ko": "ko",
    "chinese": "zh", "chi": "zh", "zho": "zh", "zh": "zh", "mandarin": "zh",
    "russian": "ru", "rus": "ru", "ru": "ru",
    "dutch": "nl", "dut": "nl", "nld": "nl", "nl": "nl",
    "danish": "da", "dan": "da", "da": "da",
    "finnish": "fi", "fin": "fi", "fi": "fi",
    "swedish": "sv", "swe": "sv", "sv": "sv",
    "norwegian": "no", "nor": "no", "nob": "no", "no": "no",
    "polish": "pl", "pol": "pl", "pl": "pl",
    "turkish": "tr", "tur": "tr", "tr": "tr",
    "arabic": "ar", "ara": "ar", "ar": "ar",
    "hebrew": "he", "heb": "he", "he": "he",
    "hindi": "hi", "hin": "hi", "hi": "hi",
    "greek": "el", "gre": "el", "ell": "el", "el": "el",
    "czech": "cs", "cze": "cs", "ces": "cs", "cs": "cs",
    "hungarian": "hu", "hun": "hu", "hu": "hu",
    "romanian": "ro", "rum": "ro", "ron": "ro", "ro": "ro",
    "slovenian": "sl", "slv": "sl", "slovenia": "sl", "sl": "sl",
    "slovak": "sk", "slo": "sk", "slk": "sk", "sk": "sk",
    "croatian": "hr", "hrv": "hr", "hr": "hr",
    "serbian": "sr", "srp": "sr", "sr": "sr",
    "bulgarian": "bg", "bul": "bg", "bg": "bg",
    "ukrainian": "uk", "ukr": "uk", "ukraine": "uk", "uk": "uk",
    "thai": "th", "tha": "th", "th": "th",
    "vietnamese": "vi", "vie": "vi", "vi": "vi",
    "indonesian": "id", "ind": "id", "id": "id",
    "malay": "ms", "may": "ms", "msa": "ms", "ms": "ms",
    "estonian": "et", "est": "et", "et": "et",
    "latvian": "lv", "lav": "lv", "lv": "lv",
    "lithuanian": "lt", "lit": "lt", "lt": "lt",
    "icelandic": "is", "ice": "is", "isl": "is", "is": "is",
    "catalan": "ca", "cat": "ca", "ca": "ca",
    "basque": "eu", "baq": "eu", "eus": "eu", "eu": "eu",
    "galician": "gl", "glg": "gl", "gl": "gl",
    "persian": "fa", "per": "fa", "fas": "fa", "fa": "fa",
    "bengali": "bn", "ben": "bn", "bn": "bn",
    "tamil": "ta", "tam": "ta", "ta": "ta",
    "telugu": "te", "tel": "te", "te": "te",
    "filipino": "fil", "fil": "fil", "tagalog": "tl", "tgl": "tl", "tl": "tl",
}

# ISO-639-1 -> ISO-639-2/B. Plex accepts either, so the three-letter form is a
# valid second slot when one language has more than one subtitle track.
ISO_639_2B = {
    "en": "eng", "es": "spa", "pt": "por", "fr": "fre", "de": "ger", "it": "ita",
    "ja": "jpn", "ko": "kor", "zh": "chi", "ru": "rus", "nl": "dut", "da": "dan",
    "fi": "fin", "sv": "swe", "no": "nor", "pl": "pol", "tr": "tur", "ar": "ara",
    "he": "heb", "hi": "hin", "el": "gre", "cs": "cze", "hu": "hun", "ro": "rum",
    "sl": "slv", "sk": "slo", "hr": "hrv", "sr": "srp", "bg": "bul", "uk": "ukr",
    "th": "tha", "vi": "vie", "id": "ind", "ms": "may", "et": "est", "lv": "lav",
    "lt": "lit", "is": "ice", "ca": "cat", "eu": "baq", "gl": "glg", "fa": "per",
    "bn": "ben", "ta": "tam", "te": "tel", "tl": "tgl",
}

ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name: str) -> str:
    """Make a component safe on APFS, SMB and NTFS without losing meaning."""
    name = name.replace("/", "-").replace(":", " -")
    name = ILLEGAL_CHARS.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.rstrip(". ")
    return name or "Untitled"


def is_dotted(stem: str) -> bool:
    """A scene-style name: dots used where spaces belong."""
    dots, spaces = stem.count("."), stem.count(" ")
    if dots >= 2 and spaces == 0:
        return True
    return dots >= 3 and dots > spaces


def normalize_separators(stem: str) -> str:
    if is_dotted(stem):
        stem = stem.replace(".", " ")
    stem = stem.replace("_", " ")
    return re.sub(r"\s+", " ", stem).strip()


def strip_bracket_junk(name: str) -> str:
    prev = None
    while prev != name:
        prev = name
        name = BRACKET_JUNK.sub(" ", name)
    return re.sub(r"\s+", " ", name).strip()


EXPLICIT_EDITION_RE = re.compile(r"(?i)\{edition-([^}]{1,32})\}")


def explicit_edition(name: str):
    """Return (name without the tag, edition text) for an existing {edition-..}."""
    m = EXPLICIT_EDITION_RE.search(name)
    if not m:
        return name, None
    return EXPLICIT_EDITION_RE.sub(" ", name).strip(), m.group(1).strip()


def extract_editions(name: str):
    """Return (name_without_edition_words, [edition names])."""
    found = []
    for pattern, label in EDITION_PATTERNS:
        if re.search(pattern, name):
            if label not in found:
                found.append(label)
            name = re.sub(pattern, " ", name)
    return re.sub(r"\s+", " ", name).strip(), found


def _year_match(name: str):
    """Prefer a parenthesised year, else the last plausible bare year."""
    m = YEAR_PAREN_RE.search(name)
    if m:
        return m
    found = list(YEAR_RE.finditer(name))
    return found[-1] if found else None


def extract_year(name: str):
    m = _year_match(name)
    return int(m.group(1)) if m else None


def split_at_year(name: str):
    """Return (what stands before the year, what follows it).

    A release name carries the title up to the year and release information
    after it, so the title is read from the first half only. Returns
    (None, None) when there is no year, or nothing but the year in front.
    """
    m = _year_match(name)
    if not m or not any(c.isalnum() for c in name[:m.start()]):
        return None, None
    return name[:m.start()], name[m.end():]


# A site that signs its uploads in front of the title: "[Site.tld] Title".
SITE_PREFIX_RE = re.compile(
    r"^\s*\[\s*(?:www\.)?[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,6}\s*\]\s*[-_.\s]*")


def strip_site_prefix(name: str) -> str:
    stripped = SITE_PREFIX_RE.sub("", name)
    return stripped if stripped.strip() else name


def extract_episode(name: str):
    """Return (season, episode, episode_end, matched_span) or None."""
    m = SXXEXX_RE.search(name)
    if m:
        return (int(m.group(1)), int(m.group(2)),
                int(m.group(3)) if m.group(3) else None, m.span())
    m = NXNN_RE.search(name)
    if m:
        return int(m.group(1)), int(m.group(2)), None, m.span()
    ms, me = SEASON_WORD_RE.search(name), EPISODE_WORD_RE.search(name)
    if ms and me:
        span = (min(ms.start(), me.start()), max(ms.end(), me.end()))
        return int(ms.group(1)), int(me.group(1)), None, span
    return None


def _is_stop_token(tok: str) -> bool:
    low = tok.lower().strip("[](){}-,")
    if not low:
        return False
    if low in STOP_TOKENS:
        return True
    if re.fullmatch(r"(?i)(?:aac|ac3|eac3|ddp?|dts|truehd)\d?\.?\d?", low):
        return True
    if re.fullmatch(r"\d\.\d", low) and low not in {"1.0"}:
        return True  # channel layouts like 5.1 / 7.1
    return False


def strip_release_tail(name: str) -> str:
    """Drop the release tail.

    A single stop token is not enough: real titles contain words like "Red" and
    "Web". Cut only where the tokens go dense, meaning two of the next three are
    release tokens, or the tail is a single trailing stop token.
    """
    m = _release_marker(name)
    if m:
        name = name[: m.start()].rstrip(" -[(_.")
    tokens = name.split(" ")
    flags = [_is_stop_token(t) for t in tokens]
    def _is_scrap(tok: str) -> bool:
        stripped = tok.strip("-_.[]() ")
        return len(stripped) <= 3 or not any(c.isalnum() for c in stripped)

    cut = None
    for i, is_stop in enumerate(flags):
        if not is_stop:
            continue
        window = flags[i:i + 3]
        tail = tokens[i + 1:]
        if (sum(window) >= 2
                or i == len(tokens) - 1
                or all(_is_scrap(t) or flags[i + 1 + j] for j, t in enumerate(tail))):
            cut = i
            break
    if cut is None:
        return name.strip(" -_.")
    out = " ".join(tokens[:cut]).strip(" -_.")
    return out or name.strip(" -_.")


BRACKETS = {"(": ")", "[": "]", "{": "}"}
OPENER_OF = {c: o for o, c in BRACKETS.items()}
SEPARATORS = " -._"


def _partners(text: str) -> dict:
    """Each bracket in text that has a partner, mapped to the partner's position."""
    open_at, partner = [], {}
    for i, ch in enumerate(text):
        if ch in BRACKETS:
            open_at.append(i)
        elif ch in OPENER_OF and open_at and text[open_at[-1]] == OPENER_OF[ch]:
            j = open_at.pop()
            partner[i], partner[j] = j, i
    return partner


def _trim_start(text: str) -> str:
    if not text or (text[0] not in BRACKETS and text[0] not in OPENER_OF):
        return text
    end = _partners(text).get(0)
    if end is None:
        return text[1:]
    if not any(c.isalnum() for c in text[1:end]):
        return text[end + 1:]
    return text[0] + text[1:].lstrip(SEPARATORS)


def _trim_end(text: str) -> str:
    if not text or (text[-1] not in BRACKETS and text[-1] not in OPENER_OF):
        return text
    start = _partners(text).get(len(text) - 1)
    if start is None:
        return text[:-1]
    if not any(c.isalnum() for c in text[start + 1:-1]):
        return text[:start]
    return text[:-1].rstrip(SEPARATORS) + text[-1]


def trim_ends(text: str) -> str:
    """Trim separators and brackets off both ends of a title, never its inside.

    A bracket goes when it has no partner, a pair when it holds no letter or
    digit, and separators just inside a pair that stays: "Glazing (Cont'd)"
    keeps its ")". The inside stays as it is because a course lesson is
    written as it was found, and the name must read back the same.
    """
    while True:
        before = text
        text = _trim_end(_trim_start(text.strip(SEPARATORS)).strip(SEPARATORS))
        if text == before:
            return text


def _capitalise(word: str) -> str:
    """Capitalise the first letter after any opening bracket: "(live" gives "(Live"."""
    lead = len(word) - len(word.lstrip("([{"))
    return word[:lead] + word[lead:].capitalize()


def smart_title(text: str) -> str:
    """Title-case only when the source is shouting; otherwise leave casing alone."""
    letters = [c for c in text if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        small = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "nor",
                 "of", "on", "or", "the", "to", "up", "vs", "via", "y", "de", "la"}
        words = text.lower().split()
        out = []
        for i, w in enumerate(words):
            out.append(w if (0 < i < len(words) - 1 and w in small) else _capitalise(w))
        return " ".join(out)
    return text


def clean_title(raw: str) -> str:
    name = normalize_separators(raw)
    name = re.sub(r"\{[^}]*\}", " ", name)
    name = strip_bracket_junk(name)
    name = strip_release_tail(name)
    name = re.sub(r"(?i)(?<![a-z0-9])s\s?\d{1,2}\s*$", " ", name)
    name = re.sub(r"(?i)\b(?:complete\s+)?(?:season|series)\s*\d{1,2}\s*$", " ", name)
    name = re.sub(r"\(\s*\)", " ", name)
    name = re.sub(r"[\s._-]+$", "", name)
    name = re.sub(r"\s+", " ", name).strip(" -_.")
    return smart_title(name)


def normalize_lang(raw: str):
    """Map a subtitle filename fragment to (lang_code, forced, sdh, leftover_title)."""
    text = raw.strip()
    forced = bool(re.search(r"(?i)\bforced\b", text))
    # "hi" is the ISO code for Hindi. Release tools write the hearing-impaired
    # marker in caps ("SDH eng HI"), so only an uppercase HI counts as the flag;
    # a lowercase hi stays Hindi, which keeps a second pass from rewriting it.
    sdh = bool(re.search(r"(?i)\b(sdh|hearing.?impaired|cc)\b", text)) \
        or bool(re.search(r"\bHI\b", text))
    text = re.sub(r"\bHI\b", " ", text)
    text = re.sub(r"(?i)\b(forced|sdh|hearing.?impaired|cc)\b", " ", text)
    text = re.sub(r"[()\[\]]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-_")
    lang = None
    # locale forms such as en-US or pt_BR are not ISO-639, keep the language half
    text = re.sub(r"(?i)\b([a-z]{2,3})[-_]([A-Za-z]{2})\b",
                  lambda m: m.group(1) if m.group(1).lower() in LANG_NAMES else m.group(0),
                  text)
    parts = [p for p in re.split(r"[.\s_-]+", text) if p]
    for part in reversed(parts):
        cand = LANG_NAMES.get(part.lower())
        if cand:
            lang = cand
            parts.remove(part)
            break
    if lang is None:
        joined = " ".join(parts).lower()
        if joined in LANG_NAMES:
            lang, parts = LANG_NAMES[joined], []
    if lang is None:
        # names arrive mangled ("Englisheng.srt"); look for a language name inside
        squashed = re.sub(r"[^a-z]", "", " ".join(parts).lower())
        best = ""
        for name, code in LANG_NAMES.items():
            if len(name) >= 5 and name in squashed and len(name) > len(best):
                best, lang = name, code
        if best:
            parts = [p for p in parts
                     if re.sub(r"[^a-z]", "", p.lower()) not in {best, best + "eng"}]
            parts = [p for p in parts if best not in re.sub(r"[^a-z]", "", p.lower())]
    leftover = " ".join(parts).strip()
    # release tools prefix subtitle files with an index (19_Hebrew.srt); that
    # index is not a title, so drop it rather than baking it into the name
    leftover = re.sub(r"^\d{1,3}\b[\s._-]*", "", leftover).strip(" .-_")
    if leftover.isdigit():
        leftover = ""
    return lang, forced, sdh, leftover


def strip_accents_key(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", text.lower())
