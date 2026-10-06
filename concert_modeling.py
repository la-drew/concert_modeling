#!/usr/bin/env python3
"""
Resale ticket price collector: SeatGeek (resale market) + Ticketmaster (primary).

Snapshots into SQLite, building a panel you can model later:
    log(price_it) ~ event FE + f(hours_to_show_it) + log(listings_it) + primary_status_it

  - SeatGeek Platform API: resale price + inventory stats per event (the outcome).
  - Ticketmaster Discovery API: primary on-sale status + face-value price range
    (a feature: while primary is selling, it caps what resale can charge).

Setup
  1. Free SeatGeek client_id:      https://seatgeek.com/account/develop
     Free Ticketmaster API key:    https://developer.ticketmaster.com  (optional)
  2. export SEATGEEK_CLIENT_ID=...
     export TICKETMASTER_API_KEY=...
  3. pip install requests
  4. python ticket_collector.py                  # take a snapshot
     python ticket_collector.py --force          # record every SeatGeek event now
     python ticket_collector.py --export out.csv # dump the joined panel to CSV

Cadence
  Schedule it HOURLY. SeatGeek: each event every 6h, hourly in its final week.
  Ticketmaster: every run (cheap, and you want to catch status flips quickly).
  Crontab:  0 * * * * cd /path/to/dir && /usr/bin/python3 concert_modeling.py >> collector.log 2>&1
"""
import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests

SG_API = "https://api.seatgeek.com/2/events"
TM_API = "https://app.ticketmaster.com/discovery/v2/events.json"
DB_PATH = os.environ.get("TICKETS_DB", "tickets.db")
FINAL_WEEK_HOURS = 168
BASE_INTERVAL_HOURS = 6
SG_STATS = ["listing_count", "visible_listing_count", "lowest_price",
            "median_price", "average_price", "highest_price"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY,
    title TEXT, datetime_utc TEXT, datetime_local TEXT,
    venue TEXT, city TEXT, state TEXT, country TEXT,
    capacity INTEGER, url TEXT, first_seen_utc TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_utc TEXT, event_id INTEGER, hours_to_show REAL,
    listing_count INTEGER, visible_listing_count INTEGER,
    lowest_price REAL, median_price REAL, average_price REAL, highest_price REAL,
    status TEXT, raw_stats TEXT,
    PRIMARY KEY (snapshot_utc, event_id)
);
CREATE TABLE IF NOT EXISTS tm_snapshots (
    snapshot_utc TEXT, tm_event_id TEXT, name TEXT,
    local_date TEXT, city TEXT, venue TEXT,
    status_code TEXT, price_min REAL, price_max REAL, currency TEXT,
    raw_price_ranges TEXT, url TEXT,
    PRIMARY KEY (snapshot_utc, tm_event_id)
);
"""


def get_json(url, params, retries=3):
    """GET with exponential backoff on network errors, 429s, and 5xx."""
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise
            wait = 2 ** (attempt + 1)
            print(f"  retrying in {wait}s ({e})", file=sys.stderr)
            time.sleep(wait)


# ---------------------------------------------------------------- SeatGeek --

def fetch_sg_events(slug, client_id):
    events, page = [], 1
    while True:
        data = get_json(SG_API, {"performers.slug": slug, "per_page": 100,
                                 "page": page, "client_id": client_id})
        events.extend(data.get("events", []))
        meta = data.get("meta", {})
        if page * meta.get("per_page", 100) >= meta.get("total", 0):
            return events
        page += 1


def should_record(hours_to_show, now, force):
    if hours_to_show < 0:
        return False                      # show already happened
    if force or hours_to_show <= FINAL_WEEK_HOURS:
        return True
    return now.hour % BASE_INTERVAL_HOURS == 0


def sg_snapshot(conn, now, ts, slug, client_id, force=False):
    events = fetch_sg_events(slug, client_id)
    if not events:
        sys.exit(f"No SeatGeek events for slug '{slug}'. Look it up at "
                 f"https://api.seatgeek.com/2/performers?q=young+miko&client_id=...")
    recorded = 0
    for e in events:
        show = datetime.fromisoformat(e["datetime_utc"]).replace(tzinfo=timezone.utc)
        hours = round((show - now).total_seconds() / 3600, 2)
        v = e.get("venue", {})
        conn.execute("""
            INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_id) DO UPDATE SET
                datetime_utc=excluded.datetime_utc,
                datetime_local=excluded.datetime_local,
                venue=excluded.venue, capacity=excluded.capacity""",
            (e["id"], e.get("title"), e["datetime_utc"], e.get("datetime_local"),
             v.get("name"), v.get("city"), v.get("state"), v.get("country"),
             v.get("capacity"), e.get("url"), ts))
        if not should_record(hours, now, force):
            continue
        s = e.get("stats", {}) or {}
        conn.execute(
            "INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ts, e["id"], hours, *[s.get(f) for f in SG_STATS],
             e.get("status"), json.dumps(s)))
        recorded += 1
        print(f"  SG {v.get('city', '?'):<16} {hours:>7.1f}h out  "
              f"low ${s.get('lowest_price')}  med ${s.get('median_price')}  "
              f"listings {s.get('listing_count')}")
    print(f"  SeatGeek: recorded {recorded}/{len(events)} events")


# ------------------------------------------------------------ Ticketmaster --

def fetch_tm_events(keyword, api_key):
    events, page = [], 0
    while True:
        data = get_json(TM_API, {"apikey": api_key, "keyword": keyword,
                                 "classificationName": "music",
                                 "size": 200, "page": page})
        events.extend(data.get("_embedded", {}).get("events", []))
        if page + 1 >= data.get("page", {}).get("totalPages", 1):
            return events
        page += 1


def is_headline_show(ev, keyword):
    """Keep the artist's own shows; drop parking passes and keyword false hits."""
    kw = keyword.lower()
    name = ev.get("name", "").lower()
    if "parking" in name:
        return False
    attractions = ev.get("_embedded", {}).get("attractions", [])
    if attractions:                       # trust the performer tag when it exists
        return any(kw == a.get("name", "").lower() for a in attractions)
    return kw in name


def pick_price_range(ranges):
    """Prefer the 'standard' range; fall back to all ranges."""
    std = [r for r in ranges if r.get("type") == "standard"] or ranges
    mins = [r["min"] for r in std if r.get("min") is not None]
    maxs = [r["max"] for r in std if r.get("max") is not None]
    return (min(mins) if mins else None,
            max(maxs) if maxs else None,
            std[0].get("currency") if std else None)


def tm_snapshot(conn, now, ts, keyword, api_key):
    events = [e for e in fetch_tm_events(keyword, api_key) if is_headline_show(e, keyword)]
    prev = {row[0]: row[1:] for row in conn.execute("""
        SELECT tm_event_id, status_code, price_min, price_max FROM tm_snapshots t
        WHERE snapshot_utc = (SELECT MAX(snapshot_utc) FROM tm_snapshots
                              WHERE tm_event_id = t.tm_event_id AND snapshot_utc < ?)""",
        (ts,))}
    recorded = 0
    for e in events:
        start = e.get("dates", {}).get("start", {})
        if start.get("dateTime"):
            show = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
            if show < now:
                continue
        venue = (e.get("_embedded", {}).get("venues") or [{}])[0]
        status = e.get("dates", {}).get("status", {}).get("code")
        ranges = e.get("priceRanges", [])
        pmin, pmax, cur = pick_price_range(ranges)
        conn.execute(
            "INSERT OR IGNORE INTO tm_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, e["id"], e.get("name"), start.get("localDate"),
             venue.get("city", {}).get("name"), venue.get("name"),
             status, pmin, pmax, cur, json.dumps(ranges), e.get("url")))
        recorded += 1
        old = prev.get(e["id"])
        if old and old != (status, pmin, pmax):
            print(f"  ** TM CHANGE {start.get('localDate')} "
                  f"{venue.get('city', {}).get('name')}: "
                  f"status {old[0]} -> {status}, range {old[1]}-{old[2]} -> {pmin}-{pmax}")
    print(f"  Ticketmaster: recorded {recorded} events")


# ----------------------------------------------------------------- export --

def export(conn, path):
    cur = conn.execute("""
        SELECT s.snapshot_utc, s.event_id, e.city, e.venue, e.datetime_local,
               s.hours_to_show, s.listing_count, s.visible_listing_count,
               s.lowest_price, s.median_price, s.average_price, s.highest_price,
               s.status, t.tm_status, t.tm_price_min, t.tm_price_max
        FROM snapshots s
        JOIN events e USING (event_id)
        LEFT JOIN (
            SELECT snapshot_utc, local_date,
                   GROUP_CONCAT(DISTINCT status_code) AS tm_status,
                   MIN(price_min) AS tm_price_min, MAX(price_max) AS tm_price_max
            FROM tm_snapshots GROUP BY snapshot_utc, local_date
        ) t ON t.snapshot_utc = s.snapshot_utc
           AND t.local_date = substr(e.datetime_local, 1, 10)
        ORDER BY s.event_id, s.snapshot_utc""")
    rows = cur.fetchall()
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([d[0] for d in cur.description])
        w.writerows(rows)
    print(f"Exported {len(rows)} rows to {path}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--slug", default="young-miko", help="SeatGeek performer slug")
    p.add_argument("--tm-keyword", default="Young Miko", help="Ticketmaster search keyword")
    p.add_argument("--force", action="store_true", help="record all SeatGeek events this run")
    p.add_argument("--export", metavar="CSV", help="export joined panel to CSV and exit")
    args = p.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    if args.export:
        export(conn, args.export)
        return

    sg_id = os.environ.get("SEATGEEK_CLIENT_ID")
    tm_key = os.environ.get("TICKETMASTER_API_KEY")
    if not sg_id:
        sys.exit("Set SEATGEEK_CLIENT_ID first (free at seatgeek.com/account/develop).")

    # One floored timestamp per run so SeatGeek and Ticketmaster rows join cleanly.
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    ts = now.isoformat()
    print(ts)

    sg_snapshot(conn, now, ts, args.slug, sg_id, force=args.force)
    conn.commit()

    if tm_key:
        try:                              # a Ticketmaster failure never costs the SeatGeek row
            tm_snapshot(conn, now, ts, args.tm_keyword, tm_key)
            conn.commit()
        except requests.RequestException as e:
            print(f"  Ticketmaster skipped: {e}", file=sys.stderr)
    else:
        print("  Ticketmaster skipped (TICKETMASTER_API_KEY not set)")


if __name__ == "__main__":
    main()
