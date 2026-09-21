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

    # Synthetic baseline: parsed once and cached (see _csv_arrays), instead of
    # re-reading the file and parsing 1000 dates row-by-row on every call.
    # Ages are still computed against "now" each time, so recency weighting
    # is unchanged. Profiled: this was ~40-146 ms of a ~61 ms-146 ms call.
    lat, lon, weight, day = _csv_arrays(category)
    if len(lat):
        today = np.datetime64(now.date(), "D").astype(np.int64)
        age = np.maximum(today - day, 0)
        rows.append(np.column_stack([lat, lon, weight, age]))

    conn = get_connection()
    live = conn.execute(
        "SELECT latitude, longitude, type, predicted_severity, timestamp FROM incidents "
        "WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
    ).fetchall()
    conn.close()
    live_rows = []
    for r in live:
        if category and TYPE_CATEGORY.get(r["type"]) != category:
            continue
        try:
            # fromisoformat accepts "YYYY-MM-DD HH:MM" and is much cheaper than strptime
            dt = datetime.fromisoformat(r["timestamp"])
        except (ValueError, TypeError):
            dt = now
        age = max((now - dt).days, 0)
        # live, verified-by-the-system reports count for more than the synthetic baseline
        w = SEVERITY_WEIGHT.get(r["predicted_severity"], 1.0) * 1.5
        live_rows.append((r["latitude"], r["longitude"], w, age))
    if live_rows:
        rows.append(np.array(live_rows, dtype=float))

    return np.vstack(rows) if rows else np.zeros((0, 4))


_CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "synthetic_incidents.csv"
_csv_cache = {"key": None, "by_category": {}}


def _csv_arrays(category: str | None):
    """(lat, lon, weight, day_number) numpy arrays for the synthetic baseline,
    optionally filtered to one category. Cached; invalidated automatically
    when the file's mtime/size changes (regenerating the dataset takes effect
    on the next call, no restart needed)."""
    empty = tuple(np.zeros(0) for _ in range(4))
    if not _CSV_PATH.exists():
        return empty
    stat = _CSV_PATH.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    if _csv_cache["key"] != key:
        df = pd.read_csv(_CSV_PATH)
        # days since 1970-01-01, vectorised; strict like the old strptime (bad dates raise)
        days = pd.to_datetime(df["date"], format="%Y-%m-%d").values.astype("datetime64[D]").astype(np.int64)
        _csv_cache["key"] = key
        _csv_cache["by_category"] = {
            "_all": (
                df["latitude"].to_numpy(float), df["longitude"].to_numpy(float),
                df["severity"].map(SEVERITY_WEIGHT).fillna(1.0).to_numpy(float), days,
            ),
            "_category": df["category"].to_numpy(),
        }
    arrays = _csv_cache["by_category"]["_all"]
    if not category:
        return arrays
    mask = _csv_cache["by_category"]["_category"] == category
    return tuple(a[mask] for a in arrays)


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


def _kernel_value_at(lat, lon, points, weighted, spatial_bandwidth_km):
    """Raw (unnormalized) kernel sum at one point - the same per-cell math
    build_risk_grid() runs, evaluated at a single coordinate instead of
    every cell in a grid."""
    dlat_km = (points[:, 0] - lat) * KM_PER_DEG_LAT
    dlon_km = (points[:, 1] - lon) * KM_PER_DEG_LON
    dist_km = np.sqrt(dlat_km ** 2 + dlon_km ** 2)
    spatial_kernel = np.exp(-0.5 * (dist_km / spatial_bandwidth_km) ** 2)
    return float(np.sum(weighted * spatial_kernel))


def _max_kernel_value(points, weighted, spatial_bandwidth_km, chunk: int = 512) -> float:
    """Max of the raw kernel sum evaluated AT each point (the normaliser).
    Same maths as calling _kernel_value_at for every point, but as chunked
    numpy blocks instead of a Python loop (~19 ms -> ~1 ms at 352 points);
    chunking bounds memory to chunk x n instead of n x n."""
    lat, lon = points[:, 0], points[:, 1]
    best = 0.0
    for i in range(0, len(points), chunk):
        dlat = (lat[i:i + chunk, None] - lat[None, :]) * KM_PER_DEG_LAT
        dlon = (lon[i:i + chunk, None] - lon[None, :]) * KM_PER_DEG_LON
        kernel = np.exp(-0.5 * (np.sqrt(dlat ** 2 + dlon ** 2) / spatial_bandwidth_km) ** 2)
        best = max(best, float((kernel * weighted[None, :]).sum(axis=1).max()))
    return best


def point_risk(lat: float, lon: float, incident_type: str | None = None,
                half_life_days: float = 30.0, spatial_bandwidth_km: float = 6.0):
    """
    Risk at any (lat, lon) - not restricted to a fixed list of places. This
    is the single risk model the whole system uses now (prediction, incident
    scoring, and routing all read from the same surface).

    Deliberately does NOT call build_risk_grid() - profiled against the
    real dev database, building the full 100x100 national grid to read
    back a single cell was the dominant cost of every report's scoring
    pipeline (~326ms of ~337ms measured, see docs/architecture.md), 10,000
    cells computed to answer a question about one point. This evaluates
    the same kernel directly at the query point instead.

    The one thing that required the full grid was normalization - risk is
    scored relative to the current data's own maximum, not an absolute
    probability (see build_risk_grid's docstring), and computing an exact
    maximum needs to check somewhere. This approximates it by evaluating
    the kernel at each historical point's own location instead of a full
    grid - for a sum-of-kernels surface, the maximum sits at or very near
    a point of high stacked density, which is exactly what a historical
    point location is. That's O(n^2) in the number of historical points
    rather than O(grid_cells x n) - cheaper at realistic near-term scale
    (crosses over around n~10,000 points, well beyond where this project
    is), and doesn't need a grid at all. Given the model was already
    documented as relative/uncalibrated rather than an absolute
    probability, this approximation doesn't change what the number means,
    just how it's computed.
    """
    category = TYPE_CATEGORY.get(incident_type) if incident_type else None
    points = _load_points(category)
    if len(points) == 0:
        return 0.0, severity_bucket(0.0)

    decay_lambda = np.log(2) / half_life_days
    recency_kernel = np.exp(-decay_lambda * points[:, 3])
    weighted = points[:, 2] * recency_kernel

    raw_value = _kernel_value_at(lat, lon, points, weighted, spatial_bandwidth_km)
    max_value = _max_kernel_value(points, weighted, spatial_bandwidth_km)
    if max_value <= 0:
        return 0.0, severity_bucket(0.0)

    value = min(raw_value / max_value, 1.0)
    return value, severity_bucket(value)


def risk_cells(lat_min: float, lon_min: float, lat_max: float, lon_max: float,
               category: str | None = None, size: int = 40, min_risk: float = 0.05,
               half_life_days: float = 30.0, spatial_bandwidth_km: float = 6.0):
    """Risk for a size x size grid of cell centres inside a box, as a sparse
    list of {lat, lon, risk} (cells below min_risk are dropped) - the data
    behind the dashboard's risk-surface layer.

    Deliberately NOT built on build_risk_grid(): that normalises by the
    maximum inside the requested box, so every viewport would show a red
    hotspot even where nothing has happened. This uses the same normaliser as
    point_risk() (the maximum over ALL points), so a cell's value equals
    point_risk() at its centre and colours mean the same thing at every zoom
    and pan. tests/test_api.py asserts that equality."""
    points = _load_points(category)
    if len(points) == 0:
        return []
    weighted = points[:, 2] * np.exp(-(np.log(2) / half_life_days) * points[:, 3])
    max_value = _max_kernel_value(points, weighted, spatial_bandwidth_km)
    if max_value <= 0:
        return []

    lat_c = lat_min + (np.arange(size) + 0.5) * (lat_max - lat_min) / size
    lon_c = lon_min + (np.arange(size) + 0.5) * (lon_max - lon_min) / size
    grid_lat, grid_lon = np.meshgrid(lat_c, lon_c, indexing="ij")
    flat_lat, flat_lon = grid_lat.ravel(), grid_lon.ravel()

    raw = np.empty(flat_lat.shape)
    chunk = 256
    for i in range(0, len(flat_lat), chunk):
        dlat = (flat_lat[i:i + chunk, None] - points[None, :, 0]) * KM_PER_DEG_LAT
        dlon = (flat_lon[i:i + chunk, None] - points[None, :, 1]) * KM_PER_DEG_LON
        kernel = np.exp(-0.5 * (np.sqrt(dlat ** 2 + dlon ** 2) / spatial_bandwidth_km) ** 2)
        raw[i:i + chunk] = (kernel * weighted[None, :]).sum(axis=1)

    risk = np.minimum(raw / max_value, 1.0)
    keep = risk >= min_risk
    return [
        {"lat": float(a), "lon": float(b), "risk": round(float(r), 3)}
        for a, b, r in zip(flat_lat[keep], flat_lon[keep], risk[keep])
    ]
