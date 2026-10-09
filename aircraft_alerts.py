#!/usr/bin/env python3
"""
Rare aircraft alerts: NASA research planes, military, government and oddities near you.

Data:   ADSB.lol public API (free, no key)  https://api.adsb.lol
Lists:  sdr-enthusiasts/plane-alert-db      (curated "interesting aircraft" list)

Usage:
    python aircraft_alerts.py            # run forever, polling every POLL_SECONDS
    python aircraft_alerts.py --once     # single check (good for cron)
    python aircraft_alerts.py --learn    # log everything, no alerts (build your mute list)
    python aircraft_alerts.py --minutes 4  # poll for 4 minutes, then exit (GitHub Actions)

Requires: pip install requests
"""

import csv
import io
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ----------------------------------------------------------------------------
# CONFIG - edit these
# ----------------------------------------------------------------------------
# Your location comes from the HOME_LAT / HOME_LON repository secrets on GitHub,
# so it never appears in the code.
HOME_LAT = float(os.environ["HOME_LAT"])
HOME_LON = float(os.environ["HOME_LON"])
RADIUS_NM = 60            # search radius in nautical miles (API max is 250)
POLL_SECONDS = 90         # be polite to the free API; don't go below ~30
COOLDOWN_HOURS = 6        # don't re-alert on the same aircraft within this window

# Callsign prefixes you always want (NASA flights usually file "NASA###")
WATCH_CALLSIGN_PREFIXES = ["NASA"]

# Specific aircraft you always want, by ICAO hex (lowercase). Add your own finds.
WATCH_HEX = set()

# plane-alert-db categories to alert on (the CSV's "Category" column).
WATCH_CATEGORIES = {
    "Oxcart",                     # spy & surveillance planes
    "Gunship",                    # A-10s, AC-130 gunships
    "United States Navy",         # Navy
    "United States Marine Corps", # Marines
    "Distinctive",                # Ospreys, P-8 sub hunters, NASA, "Janet" Area 51 shuttles
    "USAF",                       # all other Air Force (includes local traffic)
    "Historic",                   # warbirds
    "Aerobatic Teams",            # Thunderbirds, Flying Bulls, etc.
    "Toy Soldiers",               # US Army aircraft (Black Hawks, Apaches, Lakotas)
}

# Military helicopters (by type code) that aren't in the database, e.g. Utah
# Guard aircraft out of Camp Williams. Alerts if ADSB.lol flags them military.
WATCH_MILITARY_HELI_TYPES = {"H60", "H64", "H47", "H72", "UH1", "EC45"}

# Always alert on these operators, whatever their category
WATCH_OPERATORS = {"NASA"}

# Alert on *every* military-flagged aircraft (off: too many local jets)
ALERT_ALL_MILITARY = False

# If the local regulars get too noisy, add their type codes here:
MUTE_TYPES = set()        # e.g. {"F35", "K35R"}  (Hill AFB F-35s, SLC KC-135 tankers)
MUTE_HEX = set()          # specific airframes
MUTE_CALLSIGN_PREFIXES = []

# Files
BASE = Path(__file__).parent
STATE_FILE = BASE / "aircraft_seen.json"
LOG_FILE = BASE / "aircraft_log.csv"
DB_CACHE = BASE / "plane-alert-db.csv"
DB_URL = ("https://raw.githubusercontent.com/sdr-enthusiasts/"
          "plane-alert-db/main/plane-alert-db.csv")

HEADERS = {"User-Agent": "personal-aircraft-alerts/1.0"}  # API 403s without one


# ----------------------------------------------------------------------------
# NOTIFICATIONS
# ----------------------------------------------------------------------------
def send_notification(title: str, message: str, url: str) -> None:
    """Push to your phone via the free ntfy app (topic name stored as a secret)."""
    print(f"\n🔔 {title}\n{message}\n{url}\n")
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("NTFY_TOPIC not set; alert only printed to the log.")
        return
    try:
        requests.post(f"https://ntfy.sh/{topic}",
                      data=message.encode("utf-8"),
                      headers={"Title": title.encode("ascii", "ignore").decode().strip(),
                               "Click": url, "Tags": "airplane"},
                      timeout=15)
    except requests.RequestException as e:
        print(f"Notification failed: {e}")


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def distance_nm(lat1, lon1, lat2, lon2) -> float:
    r = 3440.065  # Earth radius in nautical miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def load_alert_db() -> dict:
    """Load plane-alert-db, refreshing the local copy once a day."""
    stale = (not DB_CACHE.exists()
             or time.time() - DB_CACHE.stat().st_mtime > 86400)
    if stale:
        try:
            resp = requests.get(DB_URL, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            DB_CACHE.write_text(resp.text, encoding="utf-8")
        except requests.RequestException as e:
            print(f"Couldn't refresh plane-alert-db ({e}); using cached copy.")
    if not DB_CACHE.exists():
        return {}

    db = {}
    reader = csv.DictReader(io.StringIO(DB_CACHE.read_text(encoding="utf-8")))
    for row in reader:
        hex_code = (row.get("$ICAO") or "").strip().lower()
        if hex_code:
            db[hex_code] = {
                "operator": row.get("$Operator", ""),
                "type": row.get("$Type", ""),
                "category": row.get("Category", ""),
                "tags": ", ".join(t for t in (row.get("$Tag 1"), row.get("$#Tag 2"),
                                              row.get("$#Tag 3")) if t),
            }
    return db


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    cutoff = time.time() - COOLDOWN_HOURS * 3600
    state = {k: v for k, v in state.items() if v > cutoff}  # prune old entries
    STATE_FILE.write_text(json.dumps(state))


def fetch_nearby() -> list:
    url = f"https://api.adsb.lol/v2/lat/{HOME_LAT}/lon/{HOME_LON}/dist/{RADIUS_NM}"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.json().get("ac", [])


def log_sighting(ac: dict, dist: float, reason: str) -> None:
    new = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time_utc", "hex", "callsign", "reg", "type",
                        "alt_ft", "dist_nm", "military", "reason"])
        w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ac.get("hex"), (ac.get("flight") or "").strip(), ac.get("r"),
                    ac.get("t"), ac.get("alt_baro"), round(dist, 1),
                    bool(ac.get("dbFlags", 0) & 1), reason])


def why_interesting(ac: dict, db: dict):
    """Return a reason string if this aircraft is worth an alert, else None."""
    hex_code = (ac.get("hex") or "").lower().lstrip("~")
    callsign = (ac.get("flight") or "").strip().upper()
    actype = (ac.get("t") or "").upper()

    if hex_code in MUTE_HEX or actype in MUTE_TYPES:
        return None
    if any(callsign.startswith(p) for p in MUTE_CALLSIGN_PREFIXES):
        return None

    if hex_code in WATCH_HEX:
        return "On your personal watchlist"
    if any(callsign.startswith(p) for p in WATCH_CALLSIGN_PREFIXES):
        return f"Callsign match ({callsign})"
    if hex_code in db:
        entry = db[hex_code]
        if entry["operator"] in WATCH_OPERATORS:
            return f"{entry['operator']} · {entry['category']}"
        if not WATCH_CATEGORIES or entry["category"] in WATCH_CATEGORIES:
            return f"{entry['operator']} · {entry['category']}"
    is_mil = ac.get("dbFlags", 0) & 1
    if is_mil and actype in WATCH_MILITARY_HELI_TYPES:
        return "Military helicopter"
    if ALERT_ALL_MILITARY and is_mil:
        return "Flagged military"
    return None


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def check_once(db: dict, learn: bool) -> None:
    try:
        aircraft = fetch_nearby()
    except requests.RequestException as e:
        print(f"ADSB.lol request failed: {e}")
        return

    state = load_state()
    now = time.time()

    for ac in aircraft:
        if "lat" not in ac or "lon" not in ac:
            continue
        hex_code = (ac.get("hex") or "").lower().lstrip("~")
        dist = distance_nm(HOME_LAT, HOME_LON, ac["lat"], ac["lon"])
        reason = why_interesting(ac, db)

        if learn:
            if ac.get("dbFlags", 0) & 1 or hex_code in db or reason:
                if now - state.get(hex_code, 0) > COOLDOWN_HOURS * 3600:
                    log_sighting(ac, dist, reason or "")
                    state[hex_code] = now
            continue

        if not reason or now - state.get(hex_code, 0) < COOLDOWN_HOURS * 3600:
            continue

        callsign = (ac.get("flight") or "").strip() or "no callsign"
        info = db.get(hex_code, {})
        actype = info.get("type") or ac.get("desc") or ac.get("t") or "Unknown type"
        alt = ac.get("alt_baro")
        alt_txt = "on the ground" if alt == "ground" else f"{alt:,} ft" if alt else "alt unknown"

        title = f"✈️ {actype} nearby"
        lines = [f"{callsign} · {ac.get('r') or hex_code.upper()}",
                 f"{alt_txt} · {dist:.0f} nm away",
                 reason]
        if info.get("tags"):
            lines.append(info["tags"])

        send_notification(title, "\n".join(lines),
                          f"https://adsb.lol/?icao={hex_code}")
        log_sighting(ac, dist, reason)
        state[hex_code] = now

    save_state(state)


def main() -> None:
    once = "--once" in sys.argv
    learn = "--learn" in sys.argv
    stop_at = None
    if "--minutes" in sys.argv:
        stop_at = time.time() + 60 * float(sys.argv[sys.argv.index("--minutes") + 1])
    db = load_alert_db()
    print(f"Loaded {len(db):,} aircraft from plane-alert-db. "
          f"Watching {RADIUS_NM} nm radius{' (learn mode)' if learn else ''}.")

    while True:
        check_once(db, learn)
        if once:
            break
        if stop_at and time.time() + POLL_SECONDS > stop_at:
            break
        time.sleep(POLL_SECONDS)
        if datetime.now().minute == 0:  # hourly: pick up a refreshed DB if stale
            db = load_alert_db()


if __name__ == "__main__":
    main()
