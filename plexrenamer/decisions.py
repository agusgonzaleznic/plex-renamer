"""Per-title overrides, written by hand or produced by a title lookup.

One JSON file per category in decisions/, keyed by the unit's path relative to
the volume root. Anything absent falls back to what the classifier worked out.
"""

from __future__ import annotations

import json
import os

FIELDS = ("kind", "title", "year", "ids", "editions", "note", "skip", "hide",
          "season_map")


def load(decisions_dir: str) -> dict:
    merged = {}
    if not os.path.isdir(decisions_dir):
        return merged
    # "_" prefixed files load last so hand written overrides win
    names = sorted(os.listdir(decisions_dir), key=lambda n: (n.startswith("_"), n))
    for name in names:
        if not name.endswith(".json"):
            continue
        with open(os.path.join(decisions_dir, name)) as fh:
            try:
                data = json.load(fh)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{name}: invalid JSON: {exc}") from exc
        for key, value in (data.get("units") or {}).items():
            merged.setdefault(key, {}).update(
                {k: v for k, v in value.items() if k in FIELDS})
    return merged


def apply_to(units, overrides: dict):
    """Return (kept units, number of units changed, unmatched override keys)."""
    seen, kept, changed = set(), [], 0
    for unit in units:
        override = overrides.get(unit.src_rel)
        if not override:
            kept.append(unit)
            continue
        seen.add(unit.src_rel)
        if override.get("skip") and not override.get("hide"):
            changed += 1
            continue
        if override.get("hide"):
            unit.flags.append("hidden")
            unit.notes = (unit.notes + " | " if unit.notes else "") + \
                (override.get("note") or "hidden from the scanner by decision")
            kept.append(unit)
            changed += 1
            continue
        touched = False
        if override.get("kind") and override["kind"] != unit.kind:
            unit.kind = override["kind"]
            unit.info.kind = override["kind"]
            if override["kind"] == "show" and not unit.seasons:
                # a movie folder reclassified as a show has no season map yet, and
                # without one the planner would silently emit nothing
                from .classify import collect_videos, group_seasons
                unit.seasons = group_seasons(unit.src, collect_videos(unit.src))
            touched = True
        if override.get("title"):
            unit.info.title = override["title"]
            touched = True
        if "year" in override and override["year"] != unit.info.year:
            unit.info.year = override["year"]
            touched = True
        if override.get("ids"):
            unit.info.ids.update({k: str(v) for k, v in override["ids"].items() if v})
            touched = True
        if "editions" in override:
            unit.info.editions = list(override["editions"] or [])
            touched = True
        if override.get("season_map") and unit.seasons:
            remapped = {}
            for season, videos in unit.seasons.items():
                target = override["season_map"].get(str(season), season)
                remapped.setdefault(int(target), []).extend(videos)
            unit.seasons = remapped
            touched = True
        if override.get("note"):
            unit.notes = (unit.notes + " | " if unit.notes else "") + override["note"]
        if touched:
            unit.flags = [f for f in unit.flags if f not in {"no-year", "title-suggestion"}]
            unit.flags.append("reviewed")
            changed += 1
        kept.append(unit)
    return kept, changed, sorted(set(overrides) - seen)
