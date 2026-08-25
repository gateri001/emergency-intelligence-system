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

# Bounding box covering the Nairobi-area locations used in the synthetic dataset.
LAT_MIN, LAT_MAX = -1.40, -1.00
LON_MIN, LON_MAX = 36.60, 37.15
GRID_SIZE = 40

_REFERENCE_LAT = -1.3  # for lon->km conversion; Nairobi's latitude range is small enough this holds
KM_PER_DEG_LAT = 111.0
KM_PER_DEG_LON = 111.0 * np.cos(np.radians(_REFERENCE_LAT))


def _load_points():
    """Returns an (N, 4) array of [lat, lon, weight, age_days]."""
    now = datetime.now()
    rows = []

    csv_path = Path(__file__).resolve().parent.parent / "data" / "synthetic_incidents.csv"
    if csv_path.exists():
        df = pd.read_csv(csv_path)
        for _, r in df.iterrows():
            dt = datetime.strptime(r["date"], "%Y-%m-%d")
            age = max((now - dt).days, 0)
            w = SEVERITY_WEIGHT.get(r["severity"], 1.0)
            rows.append((r["latitude"], r["longitude"], w, age))

    conn = get_connection()
    live = conn.execute(
        "SELECT latitude, longitude, predicted_severity, timestamp FROM incidents "
        "WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
    ).fetchall()
    conn.close()
    for r in live:
        try:
            dt = datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M")
        except ValueError:
            dt = now
        age = max((now - dt).days, 0)
        # live, verified-by-the-system reports count for more than the synthetic baseline
        w = SEVERITY_WEIGHT.get(r["predicted_severity"], 1.0) * 1.5
        rows.append((r["latitude"], r["longitude"], w, age))

    return np.array(rows) if rows else np.zeros((0, 4))


def build_risk_grid(half_life_days: float = 30.0, spatial_bandwidth_km: float = 1.2):
    """
    Returns (grid, lat_centers, lon_centers).
    grid[i, j] is a 0-1 normalized risk score for that cell.
    """
    lat_edges = np.linspace(LAT_MIN, LAT_MAX, GRID_SIZE + 1)
    lon_edges = np.linspace(LON_MIN, LON_MAX, GRID_SIZE + 1)
    lat_centers = (lat_edges[:-1] + lat_edges[1:]) / 2
    lon_centers = (lon_edges[:-1] + lon_edges[1:]) / 2

    grid = np.zeros((GRID_SIZE, GRID_SIZE))
    points = _load_points()
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
