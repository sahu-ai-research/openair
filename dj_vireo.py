#!/usr/bin/env python3
"""
DJ Vireo Playlist Helper 🐦🎵
Helps OpenAIR's finest avoid repetition and surface forgotten gems.

Usage: python dj_vireo.py <mode> [count] [--file path] [--no-proxy]
       python dj_vireo.py bad <artist> - <title>    Add track to quarantine
       python dj_vireo.py quarantine                  Show quarantined tracks
       python dj_vireo.py unquarantine <number>       Remove from quarantine

Modes:
  rare          Lowest total play count — the deep cuts
  cold          Lowest plays in last 30 days — cooling off
  hot           Highest plays in last 30 days — overplayed right now
  recent        Most recently played — avoid immediate repeats
  stale         Longest since last played — the forgotten ones
  overrotated   High 30d/total ratio — hammered disproportionately
  recommended   Proven tracks (>10 plays) not in Hot 25, not in Recent 25, not played in last 24h
  panic         ONE safe track right now — verified, not quarantined, artist-fresh

Count defaults to 10.

Always remember: verify before poetry :)
"""

import csv
import json
import os
import random
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

URL = "https://kurtwoloch.github.io/andon-fm-library/oa_only.json"
RAW_URL = "https://raw.githubusercontent.com/KurtWoloch/andon-fm-library/refs/heads/master/oa_only.json"
PROXY = "http://agent-container-proxy.internal:3128"
DEFAULT_FILE = "oa_only.json"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
QUARANTINE_FILE = os.path.join(SCRIPT_DIR, "quarantine.json")
CATALOG_FILE = "/home/agent119/library/openair_catalog.csv"

# Display timezone for Vireo's booth
DISPLAY_TZ = ZoneInfo("America/Los_Angeles")

# JSON timestamps are in Central European Time (auto-handles CET/CEST)
TIMESTAMP_TZ = ZoneInfo("Europe/Berlin")

MODES = {
    "rare":        "Lowest Total Plays — Deep Cuts",
    "cold":        "Lowest 30-Day Plays — Cooling Off",
    "hot":         "Highest 30-Day Plays — Maybe Ease Up",
    "recent":      "Most Recently Played — Avoid Repeats",
    "stale":       "Longest Since Last Played — Forgotten Gems",
    "overrotated": "High Recent/Total Ratio — Overrotated",
    "recommended": "Worth Playing — Popular but Not Hot",
    "panic":       "ONE Safe Track — Right Now",
}


# ── Quarantine ──────────────────────────────────────────

def load_quarantine():
    try:
        with open(QUARANTINE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_quarantine(entries):
    with open(QUARANTINE_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)


def add_quarantine(artist, title):
    entries = load_quarantine()
    key = f"{artist.lower()}|{title.lower()}"
    for e in entries:
        if e["key"] == key:
            print(f"  Already quarantined: {artist} — {title}")
            return
    entries.append({
        "key": key,
        "artist": artist,
        "title": title,
        "added": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    })
    save_quarantine(entries)
    print(f"  Quarantined: {artist} — {title}")


def show_quarantine():
    entries = load_quarantine()
    if not entries:
        print("\n  No quarantined tracks.\n")
        return
    print(f"\n  🐦 Quarantined Tracks ({len(entries)})")
    print(f"  {'=' * 50}\n")
    for i, e in enumerate(entries, 1):
        print(f"  {i:>3}. {e['artist']} — {e['title']}")
        print(f"       Added: {e['added']}")
        print()


def remove_quarantine(num):
    entries = load_quarantine()
    idx = num - 1
    if idx < 0 or idx >= len(entries):
        print(f"  Invalid number: {num} (have {len(entries)} entries)")
        return
    removed = entries.pop(idx)
    save_quarantine(entries)
    print(f"  Unquarantined: {removed['artist']} — {removed['title']}")


def is_quarantined(track, quarantine_keys):
    key = f"{track.get('artist', '').lower()}|{track.get('title', '').lower()}"
    return key in quarantine_keys


def filter_quarantined(tracks):
    q = load_quarantine()
    if not q:
        return tracks
    keys = set(e["key"] for e in q)
    return [t for t in tracks if not is_quarantined(t, keys)]


# ── Artist Distance ─────────────────────────────────────

def get_recent_artists(tracks, hours=6):
    """Get artists played within the last N hours."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    recent = set()
    for t in tracks:
        d = parse_date(t.get("last_played"))
        if d and d > cutoff:
            recent.add(t.get("artist", "").lower())
    return recent


# ── Catalog Crosswalk ───────────────────────────────────

def load_catalog():
    """Load local CSV catalog for UUID/status verification."""
    catalog = {}
    try:
        with open(CATALOG_FILE, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = f"{row.get('artist', '').strip().lower()}|{row.get('title', '').strip().lower()}"
                catalog[key] = row
        print(f"  Catalog loaded: {len(catalog)} entries from {CATALOG_FILE}")
    except FileNotFoundError:
        pass
    return catalog


def crosswalk_track(track, catalog):
    """Look up a track in the catalog. Returns (uuid, status, warnings) or None."""
    if not catalog:
        return None
    key = f"{track.get('artist', '').strip().lower()}|{track.get('title', '').strip().lower()}"
    row = catalog.get(key)
    if not row:
        return None
    return {
        "uuid": row.get("file_uuid", ""),
        "status": row.get("status", "").strip().lower(),
        "warnings": row.get("warnings", "").strip(),
    }


def is_verified(catalog_entry):
    """Check if a catalog entry is playable with no warnings."""
    if not catalog_entry:
        return False
    return catalog_entry["status"] == "playable" and not catalog_entry["warnings"]


# ── Feed Loading ────────────────────────────────────────

def load_library(source=None, skip_proxy=False):
    """Load from local file, proxy fetch, or direct URL fetch."""
    path = source or DEFAULT_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            print(f"  Loaded from local file: {path}")
            return json.load(f)
    except FileNotFoundError:
        if source:
            print(f"  File not found: {source}")
            sys.exit(1)

    if not skip_proxy:
        try:
            proxy = urllib.request.ProxyHandler({"https": PROXY, "http": PROXY})
            opener = urllib.request.build_opener(proxy)
            req = urllib.request.Request(RAW_URL, headers={"User-Agent": "DJVireoHelper/1.0"})
            with opener.open(req, timeout=15) as resp:
                print(f"  Fetched via proxy from: {RAW_URL}")
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"  Proxy fetch failed: {e}")

    print("  Trying direct URL fetch...")
    try:
        req = urllib.request.Request(URL, headers={"User-Agent": "DJVireoHelper/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"  Fetched from: {URL}")
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  Direct fetch failed: {e}")
        print(f"\n  Save the JSON manually and try again:")
        print(f"    curl -o {DEFAULT_FILE} {URL}")
        print(f"    python dj_vireo.py <mode>")
        sys.exit(1)


# ── Date Parsing ────────────────────────────────────────

def parse_date(s):
    if not s:
        return None
    try:
        clean = s.strip().rstrip("Z")
        return datetime.strptime(clean, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TIMESTAMP_TZ)
    except ValueError:
        return None


# ── Sorting & Filtering ────────────────────────────────

def sort_tracks(tracks, mode, catalog=None):
    if mode == "rare":
        return sorted(tracks, key=lambda t: t.get("play_count_total", 0))

    elif mode == "cold":
        return sorted(tracks, key=lambda t: t.get("play_count_30d", 0))

    elif mode == "hot":
        return sorted(tracks, key=lambda t: t.get("play_count_30d", 0), reverse=True)

    elif mode == "recent":
        with_dates = [t for t in tracks if parse_date(t.get("last_played"))]
        return sorted(with_dates, key=lambda t: parse_date(t["last_played"]), reverse=True)

    elif mode == "stale":
        with_dates = [t for t in tracks if parse_date(t.get("last_played"))]
        return sorted(with_dates, key=lambda t: parse_date(t["last_played"]))

    elif mode == "overrotated":
        eligible = [t for t in tracks if t.get("play_count_total", 0) > 5]
        return sorted(
            eligible,
            key=lambda t: t.get("play_count_30d", 0) / max(t.get("play_count_total", 1), 1),
            reverse=True,
        )

    elif mode == "recommended":
        now = datetime.now(timezone.utc)
        by_30d = sorted(tracks, key=lambda t: t.get("play_count_30d", 0), reverse=True)
        hot_ids = set(id(t) for t in by_30d[:25])
        with_dates = [t for t in tracks if parse_date(t.get("last_played"))]
        by_recent = sorted(with_dates, key=lambda t: parse_date(t["last_played"]), reverse=True)
        recent_ids = set(id(t) for t in by_recent[:25])
        excluded = hot_ids | recent_ids
        cutoff = now - timedelta(hours=24)
        recent_artists = get_recent_artists(tracks, hours=6)
        candidates = [
            t for t in tracks
            if id(t) not in excluded
            and t.get("play_count_total", 0) > 10
            and (parse_date(t.get("last_played")) is None or parse_date(t.get("last_played")) < cutoff)
            and t.get("artist", "").lower() not in recent_artists
        ]
        candidates = sorted(candidates, key=lambda t: t.get("play_count_total", 0), reverse=True)
        pool = candidates[:max(len(candidates) // 2, 20)]
        random.shuffle(pool)
        return pool

    elif mode == "panic":
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24)
        recent_artists = get_recent_artists(tracks, hours=6)
        candidates = [
            t for t in tracks
            if t.get("play_count_total", 0) > 10
            and (parse_date(t.get("last_played")) is None or parse_date(t.get("last_played")) < cutoff)
            and t.get("artist", "").lower() not in recent_artists
        ]
        # If catalog available, only return verified tracks
        if catalog:
            verified = [t for t in candidates if is_verified(crosswalk_track(t, catalog))]
            if verified:
                candidates = verified
        candidates = sorted(candidates, key=lambda t: t.get("play_count_total", 0), reverse=True)
        pool = candidates[:max(len(candidates) // 3, 15)]
        random.shuffle(pool)
        return pool[:1]

    return tracks


# ── Output Formatting ───────────────────────────────────

def format_output(tracks, mode, count, catalog=None):
    header = MODES.get(mode, mode)
    now = datetime.now(timezone.utc)

    print(f"\n{'=' * 60}")
    print(f"  🐦 DJ Vireo — {header}")
    print(f"{'=' * 60}\n")

    if mode == "panic" and tracks:
        t = tracks[0]
        artist = t.get("artist", "Unknown")
        title = t.get("title", "Unknown")
        total = t.get("play_count_total", 0)
        recent = t.get("play_count_30d", 0)

        d = parse_date(t.get("last_played"))
        last = t.get("last_played", "never")
        hours_ago = "?"
        if d:
            last = d.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M PT")
            h = max((now - d).total_seconds() / 3600, 0)
            if h < 1:
                hours_ago = "<1h"
            elif h < 48:
                hours_ago = f"{h:.0f}h"
            else:
                hours_ago = f"{h / 24:.0f}d"

        cat_entry = crosswalk_track(t, catalog) if catalog else None
        uuid_line = ""
        verified_str = "not checked (no catalog)"
        if cat_entry:
            uuid_line = f"\n  UUID:   {cat_entry['uuid']}"
            verified_str = "✅ verified" if is_verified(cat_entry) else f"⚠️ {cat_entry['status']}/{cat_entry['warnings']}"

        print(f"  >>> {artist} — {title}")
        print()
        print(f"  Reason: Total plays {total}, last played {hours_ago} ago,")
        print(f"          artist not heard in 6h, not quarantined.")
        print(f"  Status: {verified_str}{uuid_line}")
        print(f"  Last:   {last}")
        print(f"  30d:    {recent}  |  Total: {total}")
        print()
        print(f"  Always remember: verify before poetry :)")
        print(f"\n{'=' * 60}\n")
        return

    if not tracks:
        print(f"  No tracks found for this mode.")
        print(f"\n  Always remember: verify before poetry :)")
        print(f"\n{'=' * 60}\n")
        return

    for i, t in enumerate(tracks[:count], 1):
        artist = t.get("artist", "Unknown")
        title = t.get("title", "Unknown")
        total = t.get("play_count_total", 0)
        recent = t.get("play_count_30d", 0)
        last = t.get("last_played", "never")

        d = parse_date(t.get("last_played"))
        if d:
            last = d.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M PT")

        days_ago = ""
        if d:
            hours = max((now - d).total_seconds() / 3600, 0)
            if hours < 1:
                days_ago = f" (<1h ago)"
            elif hours < 48:
                days_ago = f" ({hours:.0f}h ago)"
            else:
                days_ago = f" ({hours / 24:.0f}d ago)"

        ratio = ""
        if mode == "overrotated" and total > 0:
            r = recent / total * 100
            ratio = f"  [{r:.0f}% of all plays in 30d]"

        print(f"  {i:>2}. {artist} — {title}")
        print(f"      Total: {total}  |  30d: {recent}  |  Last: {last}{days_ago}{ratio}")
        if catalog:
            cat_entry = crosswalk_track(t, catalog)
            if cat_entry:
                v = "✅" if is_verified(cat_entry) else f"⚠️ {cat_entry['status']}/{cat_entry['warnings']}"
                print(f"      Catalog: {v}  |  UUID: {cat_entry['uuid']}")
            else:
                print(f"      Catalog: ❌ not found")
        print()

    print(f"  Always remember: verify before poetry :)")
    print(f"\n{'=' * 60}")
    print(f"  Library size: {len(tracks)} tracks")
    if catalog:
        print(f"  Catalog: {len(catalog)} entries")
    q = load_quarantine()
    if q:
        print(f"  Quarantined: {len(q)} tracks")
    print(f"{'=' * 60}\n")


# ── Main ────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        print(f"Available modes: {', '.join(MODES.keys())}")
        print(f"\nDefault: reads '{DEFAULT_FILE}' from current directory.")
        print(f"Use --file <path> to specify a different file.")
        print(f"Use --no-proxy to skip proxy and fetch directly.")
        print(f"Falls back to URL fetch if no local file found.")
        sys.exit(0)

    # Quarantine commands
    if sys.argv[1] == "bad":
        text = " ".join(sys.argv[2:])
        if " - " not in text:
            print("  Usage: python dj_vireo.py bad <artist> - <title>")
            sys.exit(1)
        artist, title = text.split(" - ", 1)
        add_quarantine(artist.strip(), title.strip())
        sys.exit(0)

    if sys.argv[1] == "quarantine":
        show_quarantine()
        sys.exit(0)

    if sys.argv[1] == "unquarantine":
        if len(sys.argv) < 3:
            print("  Usage: python dj_vireo.py unquarantine <number>")
            sys.exit(1)
        try:
            remove_quarantine(int(sys.argv[2]))
        except ValueError:
            print(f"  Invalid number: {sys.argv[2]}")
        sys.exit(0)

    mode = sys.argv[1].lower()
    if mode not in MODES:
        print(f"Unknown mode: '{mode}'")
        print(f"Available: {', '.join(MODES.keys())}")
        sys.exit(1)

    count = 10
    source = None
    skip_proxy = False
    args = sys.argv[2:]
    while args:
        a = args.pop(0)
        if a == "--file" and args:
            source = args.pop(0)
        elif a == "--no-proxy":
            skip_proxy = True
        else:
            try:
                count = int(a)
            except ValueError:
                print(f"Invalid argument: '{a}' — ignoring")

    tracks = load_library(source, skip_proxy)
    tracks = filter_quarantined(tracks)
    catalog = load_catalog()
    sorted_tracks = sort_tracks(tracks, mode, catalog)
    format_output(sorted_tracks, mode, count, catalog)


if __name__ == "__main__":
    main()