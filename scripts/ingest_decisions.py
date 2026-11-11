"""Turn the output of a title lookup run into decisions/<Category>.json.

Reads the run's JSON-lines journal directly so a partially finished run is still
usable. Verification corrections win over the original lookup.
"""

from __future__ import annotations

import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DECISIONS = os.path.join(HERE, "decisions")


def read_journal(path):
    lookups, verdicts = [], {}
    with open(path) as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "result":
                continue
            value = rec.get("result")
            if not isinstance(value, dict):
                continue
            if "entries" in value:
                lookups.extend(value["entries"])
            elif "verdicts" in value:
                for v in value["verdicts"]:
                    if v.get("unit"):
                        verdicts.setdefault(v["unit"], []).append(v)
    return lookups, verdicts


def merge(lookups, verdicts):
    out, stats = {}, collections.Counter()
    for entry in lookups:
        unit = entry.get("unit")
        if not unit:
            stats["missing unit key"] += 1
            continue
        record = dict(entry)
        for v in verdicts.get(unit, []):
            if v.get("agrees"):
                record["verified"] = True
                continue
            stats["disputed"] += 1
            record["verified"] = False
            record["dispute"] = v.get("reason", "")
            if v.get("correctTitle"):
                record["title"] = v["correctTitle"]
                stats["title corrected"] += 1
            if v.get("correctYear"):
                record["year"] = v["correctYear"]
                stats["year corrected"] += 1
            if v.get("correctImdb"):
                record["imdb"] = v["correctImdb"]
                stats["imdb corrected"] += 1
            if v.get("correctKind"):
                record["kind"] = v["correctKind"]
        record.setdefault("verified", False)
        # a later chunk should never silently replace an earlier verified one
        if unit in out and out[unit].get("verified") and not record.get("verified"):
            continue
        out[unit] = record
        stats["units"] += 1
    return out, stats


def write(records):
    os.makedirs(DECISIONS, exist_ok=True)
    for name in os.listdir(DECISIONS):
        # files starting with "_" are hand written and outrank the generated ones
        if name.endswith(".json") and not name.startswith("_"):
            os.remove(os.path.join(DECISIONS, name))
    by_cat = collections.defaultdict(dict)
    for unit, rec in records.items():
        category = unit.split("/", 1)[0]
        ids = {}
        for provider in ("imdb", "tmdb", "tvdb"):
            value = rec.get(provider)
            if value and str(value).lower() not in {"none", "null", ""}:
                ids[provider] = str(value)
        payload = {}
        if rec.get("kind") == "skip":
            payload["skip"] = True
        else:
            if rec.get("kind"):
                payload["kind"] = rec["kind"]
            if rec.get("title"):
                payload["title"] = rec["title"]
            if rec.get("year"):
                payload["year"] = int(rec["year"])
            if ids:
                payload["ids"] = ids
            if rec.get("editions"):
                payload["editions"] = rec["editions"]
        note = " ".join(x for x in [rec.get("note", ""),
                                    ("DISPUTED: " + rec["dispute"]) if rec.get("dispute") else "",
                                    f"confidence={rec.get('confidence', '?')}",
                                    f"verified={rec.get('verified')}"] if x).strip()
        if note:
            payload["note"] = note
        by_cat[category][unit] = payload
    for category, units in by_cat.items():
        path = os.path.join(DECISIONS, category.replace(" ", "_") + ".json")
        with open(path, "w") as fh:
            json.dump({"category": category, "units": units}, fh, indent=1,
                      ensure_ascii=False)
    return {c: len(u) for c, u in by_cat.items()}


def main():
    journal = sys.argv[1]
    lookups, verdicts = read_journal(journal)
    records, stats = merge(lookups, verdicts)
    counts = write(records)
    print(f"lookup entries: {len(lookups)}   units with a verdict: {len(verdicts)}")
    print("stats:", dict(stats))
    for category in sorted(counts):
        print(f"  {category:18} {counts[category]:4} decisions")
    unverified = [u for u, r in records.items() if not r.get("verified")]
    print(f"\nunverified or disputed: {len(unverified)}")
    for unit in unverified[:25]:
        r = records[unit]
        print(f"  {unit}  ->  {r.get('title')!r} ({r.get('year')}) "
              f"{r.get('imdb') or r.get('tmdb') or '-'}  {r.get('dispute', '')[:80]}")


if __name__ == "__main__":
    main()
