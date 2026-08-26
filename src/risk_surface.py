"""
Builds a spatial risk surface over the city: a grid where each cell's risk
is a distance- and recency-weighted sum of nearby incidents (historical
synthetic baseline + live reports). This is what the router pathfinds over
- more useful than either a raw heatmap of past points, or a single
per-area classifier that can't answer "how risky is this exact spot."
"""
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from src.database import get_connection

SEVERITY_WEIGHT = {"Low": 1.0, "Medium": 2.0, "High": 3.5}

# Real Kenya bounding box (matches scripts/ingest_firms.py) - incidents
# happen nationwide, not just in Nairobi. GRID_SIZE=100 trades resolution
# (~10km/cell vs ~1.1km/cell when this was Nairobi-only) for a build that's
# still fast enough to run per-request; a real fix (only build/search a
# local window around the query point, or cache+invalidate) is flagged as
# a known follow-up, not solved here - see docs/architecture.md.
LAT_MIN, LAT_MAX = -4.72, 5.03
LON_MIN, LON_MAX = 33.5, 41.91
GRID_SIZE = 100

_REFERENCE_LAT = -0.5  # rough national-average latitude for lon->km conversion
KM_PER_DEG_LAT = 111.0
KM_PER_DEG_LON = 111.0 * np.cos(np.radians(_REFERENCE_LAT))


# Incident type -> broad category, so a query about "robbery" doesn't get
# muddied by unrelated flood history at the same point, and vice versa.
TYPE_CATEGORY = {
    "burglary": "crime", "robbery": "crime", "theft": "crime", "assault": "crime",
    "vandalism": "crime", "suspicious_activity": "crime",
    "flood": "hazard", "fire": "hazard",
    "accident": "medical", "medical_emergency": "medical",
}


def _load_points(category: str | None = None):
    """Returns an (N, 4) array of [lat, lon, weight, age_days]. No area names
    anywhere - every point is real coordinates, so this covers any location,
    not a fixed list. `category` optionally filters to one hazard family
    (crime/hazard/medical); None uses everything."""
    now = datetime.now()
    rows = []

    csv_path = Path(__file__).resolve().parent.parent / "data" / "synthetic_incidents.csv"
    if csv_path.exists():
        df = pd.read_csv(csv_path)
        if category:
            df = df[df["category"] == category]
        for _, r in df.iterrows():
            dt = datetime.strptime(r["date"], "%Y-%m-%d")
            age = max((now - dt).days, 0)
            w = SEVERITY_WEIGHT.get(r["severity"], 1.0)
            rows.append((r["latitude"], r["longitude"], w, age))

    conn = get_connection()
    live = conn.execute(
        "SELECT latitude, longitude, type, predicted_severity, timestamp FROM incidents "
        "WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
    ).fetchall()
    conn.close()
    for r in live:
        if category and TYPE_CATEGORY.get(r["type"]) != category:
            continue
        try:
            dt = datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M")
        except ValueError:
            dt = now
        age = max((now - dt).days, 0)
        # live, verified-by-the-system reports count for more than the synthetic baseline
        w = SEVERITY_WEIGHT.get(r["predicted_severity"], 1.0) * 1.5
        rows.append((r["latitude"], r["longitude"], w, age))

    return np.array(rows) if rows else np.zeros((0, 4))


def build_risk_grid(half_life_days: float = 30.0, spatial_bandwidth_km: float = 6.0,
                     category: str | None = None, bbox: tuple | None = None,
                     grid_size: int | None = None):
    """
    Returns (grid, lat_centers, lon_centers).
    grid[i, j] is a 0-1 normalized risk score for that cell, relative to the
    current data's own max - not an absolute/calibrated probability.

    `bbox` (lat_min, lon_min, lat_max, lon_max) and `grid_size` default to
    the national box/resolution, but callers that need finer detail in one
    area (safe-routing) can pass a tighter bbox with a smaller grid_size to
    get city-block resolution there without paying for it everywhere.
    """
    lat_min, lon_min, lat_max, lon_max = bbox or (LAT_MIN, LON_MIN, LAT_MAX, LON_MAX)
    size = grid_size or GRID_SIZE

    lat_edges = np.linspace(lat_min, lat_max, size + 1)
    lon_edges = np.linspace(lon_min, lon_max, size + 1)
    lat_centers = (lat_edges[:-1] + lat_edges[1:]) / 2
    lon_centers = (lon_edges[:-1] + lon_edges[1:]) / 2

    grid = np.zeros((size, size))
    points = _load_points(category)
    if len(points) == 0:
        return grid, lat_centers, lon_centers

    decay_lambda = np.log(2) / half_life_days
    recency_kernel = np.exp(-decay_lambda * points[:, 3])
    weighted = points[:, 2] * recency_kernel

    for i, la in enumerate(lat_centers):
        dlat_km = (points[:, 0] - la) * KM_PER_DEG_LAT
        for j, lo in enumerate(lon_centers):
            dlon_km = (points[:, 1] - lo) * KM_PER_DEG_LON
            dist_km = np.sqrt(dlat_km ** 2 + dlon_km ** 2)
            spatial_kernel = np.exp(-0.5 * (dist_km / spatial_bandwidth_km) ** 2)
            grid[i, j] = np.sum(weighted * spatial_kernel)

    if grid.max() > 0:
        grid = grid / grid.max()
    return grid, lat_centers, lon_centers


def severity_bucket(risk_value: float) -> str:
    if risk_value >= 0.66:
        return "High"
    if risk_value >= 0.33:
        return "Medium"
    return "Low"


def point_risk(lat: float, lon: float, incident_type: str | None = None):
    """
    Risk at any (lat, lon) - not restricted to a fixed list of places. This
    is the single risk model the whole system uses now (prediction, incident
    scoring, and routing all read from the same surface).
    """
    category = TYPE_CATEGORY.get(incident_type) if incident_type else None
    grid, lat_centers, lon_centers = build_risk_grid(category=category)
    i = int(np.argmin(np.abs(lat_centers - lat)))
    j = int(np.argmin(np.abs(lon_centers - lon)))
    value = float(grid[i, j])
    return value, severity_bucket(value)
