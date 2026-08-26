"""
Pulls real disaster events from GDACS (Global Disaster Alert and
Coordination System) for Kenya and stores them separately from the
incidents table.

Why separate: GDACS events are national-scale (a "Flood in Kenya" alert
spanning weeks, one imprecise centroid point) - very different resolution
from a citizen's point-level, single-timestamp report. Mixing the two would
distort the risk grid's spatial kernel, which assumes comparable-resolution
points. This is real, verified, external data used for corroboration and
display, not for the same training pipeline as `generate_synthetic_data.py`.

No registration required - GDACS is a free public API.
Run: python scripts/ingest_gdacs.py
"""
import sys
from datetime import date
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.database import get_connection, init_db

GDACS_URL = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
EVENT_TYPES = ["FL", "DR", "WF"]  # Flood, Drought, Wildfire - the hazard types relevant here


def fetch_events(event_type: str, from_date: str, to_date: str) -> list[dict]:
    events = []
    page = 1
    while True:
        resp = requests.get(
            GDACS_URL,
            params={"eventlist": event_type, "fromdate": from_date, "todate": to_date, "pagenumber": page},
            timeout=30,
        )
        if resp.status_code == 204 or not resp.content:
            break
        resp.raise_for_status()
        data = resp.json()
        features = data.get("features", [])
        if not features:
            break
        events.extend(features)
        page += 1
        if page > 10:  # safety cap
            break
    return events


def main():
    init_db()  # schema for external_events lives centrally in src/database.py
    conn = get_connection()

    from_date = "2015-01-01"
    to_date = date.today().isoformat()

    total_kenya = 0
    for etype in EVENT_TYPES:
        try:
            events = fetch_events(etype, from_date, to_date)
        except requests.RequestException as e:
            print(f"  [{etype}] fetch failed: {e}")
            continue

        kenya_events = [e for e in events if "Kenya" in (e["properties"].get("country") or "")]
        for e in kenya_events:
            p = e["properties"]
            coords = e.get("geometry", {}).get("coordinates", [None, None])
            conn.execute(
                """INSERT OR IGNORE INTO external_events
                   (source, event_type, country, name, latitude, longitude, from_date, to_date, alert_level)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("gdacs", etype, p.get("country"), p.get("name"), coords[1], coords[0],
                 p.get("fromdate"), p.get("todate"), p.get("alertlevel")),
            )
        print(f"  [{etype}] {len(events)} global events, {len(kenya_events)} in Kenya")
        total_kenya += len(kenya_events)

    conn.commit()
    conn.close()
    print(f"Done. {total_kenya} Kenya events found across {EVENT_TYPES}, stored in external_events.")


if __name__ == "__main__":
    main()
