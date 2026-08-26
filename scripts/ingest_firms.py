"""
Pulls real, near-real-time active fire detections from NASA FIRMS (VIIRS
sensor), filtered to Kenya. Unlike GDACS/UNOSAT, this is precise
point-level data with a real timestamp per detection - the right
resolution to feed directly into the incidents table (not kept separate
like the coarse sources).

Important caveat, deliberately not hidden: VIIRS detects ALL thermal
anomalies - wildfires, but also routine agricultural burning (very common
in East Africa) and industrial heat sources. Unfiltered, ~1,100
detections/day fall in Kenya's bounding box, median intensity (FRP) only
~5.6 MW - mostly small burns, not dangerous fires. This script filters to
FRP >= MIN_FRP and excludes "low" confidence readings, keeping the more
significant ~20% of detections. That threshold is a judgment call, not a
validated "this is definitely dangerous" cutoff - tune MIN_FRP if it's
over- or under-including.

No API key needed - HDX mirrors NASA's public, continuously-updated files.
Run: python scripts/ingest_firms.py
"""
import io
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np
import requests
import shapefile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.database import get_connection, init_db
from src.risk_surface import build_risk_grid, severity_bucket

FIRMS_24H_SHP_URL = (
    "https://firms.modaps.eosdis.nasa.gov/data/active_fire/suomi-npp-viirs-c2/"
    "shapes/zips/SUOMI_VIIRS_C2_Northern_and_Central_Africa_24h.zip"
)

# Real Kenya bounding box (not the risk grid's Nairobi-only box) - loose on
# purpose since it's a box not a border, some spillover from neighboring
# countries near the edges is expected.
KENYA_BBOX = (-4.72, 33.5, 5.03, 41.91)  # lat_min, lon_min, lat_max, lon_max
MIN_FRP = 10.0  # megawatts - see module docstring


def main():
    init_db()
    conn = get_connection()

    print(f"Downloading {FIRMS_24H_SHP_URL} ...")
    resp = requests.get(FIRMS_24H_SHP_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        shp_name = [n for n in zf.namelist() if n.endswith(".shp")][0]
        stem = shp_name[:-4]
        shp = io.BytesIO(zf.read(stem + ".shp"))
        dbf = io.BytesIO(zf.read(stem + ".dbf"))
        sf = shapefile.Reader(shp=shp, dbf=dbf)

    # Score against the existing hazard-category surface (built once, not
    # per-point) so these get the same kind of severity value as any other
    # report, consistent with how /predict and /report/* already work.
    grid, lat_centers, lon_centers = build_risk_grid(category="hazard")

    def score(lat, lon):
        i = int(np.argmin(np.abs(lat_centers - lat)))
        j = int(np.argmin(np.abs(lon_centers - lon)))
        return severity_bucket(float(grid[i, j]))

    inserted, seen = 0, 0
    for sr in sf.iterShapeRecords():
        lon, lat = sr.shape.points[0]
        if not (KENYA_BBOX[0] <= lat <= KENYA_BBOX[2] and KENYA_BBOX[1] <= lon <= KENYA_BBOX[3]):
            continue
        seen += 1
        rec = sr.record.as_dict()
        if rec["CONFIDENCE"] == "low" or rec["FRP"] < MIN_FRP:
            continue

        acq_date = rec["ACQ_DATE"]
        acq_time = str(rec["ACQ_TIME"]).zfill(4)
        timestamp = f"{acq_date} {acq_time[:2]}:{acq_time[2:]}"

        description = f"NASA FIRMS VIIRS detection, FRP={rec['FRP']} MW, confidence={rec['CONFIDENCE']}"
        conn.execute(
            """INSERT INTO incidents (source, type, area, latitude, longitude, description,
               predicted_severity, timestamp) VALUES (?, 'fire', '', ?, ?, ?, ?, ?)""",
            ("bulk", lat, lon, description, score(lat, lon), timestamp),
        )
        inserted += 1

    conn.commit()
    conn.close()
    print(f"{seen} detections in Kenya bbox, {inserted} met the significance filter "
          f"(FRP>={MIN_FRP}, confidence != low) and were inserted, scored against the "
          f"existing hazard-category risk surface.")


if __name__ == "__main__":
    main()
