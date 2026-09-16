"""
Pulls real Kenya health facility locations (hospitals, clinics, pharmacies)
from the "Kenya Healthsites" dataset on the Humanitarian Data Exchange (HDX)
- sourced from healthsites.io / OpenStreetMap, refreshed roughly every 90
days (current through late 2025 as of this writing), not a one-time
historical snapshot.

Why this exists: risk-aware safe routing (src/routing.py) answers "route me
away from danger," which is the right question for crime/flood/fire but the
wrong one for a medical emergency - what actually helps there is "route me
toward real help." This is the data that makes that second question
answerable with a real hospital, not a guess.

A meaningful fraction of the raw dataset (~26%) has no usable point
coordinates - OSM "way" (building outline) features whose centroid wasn't
computed, not every listed facility. Those rows are skipped, not
force-fit.

Unlike FIRMS (a rolling 24h snapshot, replaced every run), a hospital's
location doesn't stop being true after a day - this is a slowly-changing
registry, so re-running this script updates existing rows by osm_id rather
than deleting everything first.

Licensed CC-BY, no registration required.
Run: python scripts/ingest_health_facilities.py
"""
import csv
import io
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.database import get_connection, init_db

HDX_PACKAGE_URL = "https://data.humdata.org/api/3/action/package_show?id=kenya-healthsites"
RESOURCE_NAME = "kenya-healthsites-csv"

# Facility types actually useful for "where do I get medical help" - skips
# pharmacy/dentist/laboratory/etc, which aren't where you'd route someone
# having a medical emergency.
EMERGENCY_RELEVANT_AMENITIES = {"hospital", "clinic", "doctors"}


def main():
    init_db()
    conn = get_connection()

    headers = {"User-Agent": "Mozilla/5.0"}
    print(f"Looking up dataset resources: {HDX_PACKAGE_URL}")
    pkg = requests.get(HDX_PACKAGE_URL, headers=headers, timeout=30).json()
    resource = next(r for r in pkg["result"]["resources"] if r["name"] == RESOURCE_NAME)
    csv_url = resource["url"]

    print(f"Downloading {csv_url} ...")
    resp = requests.get(csv_url, headers=headers, timeout=60)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.content.decode("utf-8")))

    seen, inserted, updated, skipped_no_coords, skipped_irrelevant = 0, 0, 0, 0, 0
    for row in reader:
        seen += 1
        amenity = (row.get("amenity") or "").strip().lower()
        if amenity not in EMERGENCY_RELEVANT_AMENITIES:
            skipped_irrelevant += 1
            continue

        x, y = (row.get("X") or "").strip(), (row.get("Y") or "").strip()
        if not x or not y:
            skipped_no_coords += 1
            continue
        try:
            lon, lat = float(x), float(y)
        except ValueError:
            skipped_no_coords += 1
            continue

        osm_id = row.get("osm_id", "").strip()
        name = (row.get("name") or "Unnamed facility").strip()
        has_emergency = (row.get("emergency") or "").strip().lower() or None

        cur = conn.execute("SELECT id FROM health_facilities WHERE osm_id = ?", (osm_id,))
        if cur.fetchone():
            conn.execute(
                "UPDATE health_facilities SET name=?, amenity=?, has_emergency=?, latitude=?, longitude=?, "
                "fetched_at=datetime('now') WHERE osm_id=?",
                (name, amenity, has_emergency, lat, lon, osm_id),
            )
            updated += 1
        else:
            conn.execute(
                "INSERT INTO health_facilities (osm_id, name, amenity, has_emergency, latitude, longitude) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (osm_id, name, amenity, has_emergency, lat, lon),
            )
            inserted += 1

    conn.commit()
    conn.close()
    print(
        f"{seen} rows in source, {skipped_irrelevant} skipped (not hospital/clinic/doctors), "
        f"{skipped_no_coords} skipped (no usable coordinates). "
        f"{inserted} new facilities inserted, {updated} existing facilities refreshed."
    )


if __name__ == "__main__":
    main()
