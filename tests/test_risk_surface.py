"""
The point_risk() speed-up (cached CSV, vectorised normaliser, faster timestamp
parsing) must not change a single number. `_reference_*` below is the ORIGINAL
implementation kept verbatim (iterrows + strptime, Python-loop max) as an
independent oracle.
"""
import os
from datetime import datetime

import numpy as np
import pandas as pd

import src.risk_surface as rs
from src.database import get_connection


def _reference_load_points(category):
    now = datetime.now()
    rows = []
    df = pd.read_csv(rs._CSV_PATH)
    if category:
        df = df[df["category"] == category]
    for _, r in df.iterrows():
        dt = datetime.strptime(r["date"], "%Y-%m-%d")
        age = max((now - dt).days, 0)
        rows.append((r["latitude"], r["longitude"], rs.SEVERITY_WEIGHT.get(r["severity"], 1.0), age))

    conn = get_connection()
    live = conn.execute(
        "SELECT latitude, longitude, type, predicted_severity, timestamp FROM incidents "
        "WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
    ).fetchall()
    conn.close()
    for r in live:
        if category and rs.TYPE_CATEGORY.get(r["type"]) != category:
            continue
        try:
            dt = datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M")
        except ValueError:
            dt = now
        age = max((now - dt).days, 0)
        rows.append((r["latitude"], r["longitude"], rs.SEVERITY_WEIGHT.get(r["predicted_severity"], 1.0) * 1.5, age))
    return np.array(rows) if rows else np.zeros((0, 4))


def _reference_point_risk(lat, lon, incident_type):
    category = rs.TYPE_CATEGORY.get(incident_type) if incident_type else None
    points = _reference_load_points(category)
    if len(points) == 0:
        return 0.0
    weighted = points[:, 2] * np.exp(-(np.log(2) / 30.0) * points[:, 3])
    raw = rs._kernel_value_at(lat, lon, points, weighted, 6.0)
    mx = max((rs._kernel_value_at(p[0], p[1], points, weighted, 6.0) for p in points), default=0.0)
    return 0.0 if mx <= 0 else min(raw / mx, 1.0)


def _seed_live_incidents():
    conn = get_connection()
    conn.executemany(
        "INSERT INTO incidents (source, type, area, latitude, longitude, description, predicted_severity, timestamp) "
        "VALUES ('citizen', ?, '', ?, ?, '', ?, ?)",
        [
            ("flood", -1.19, 36.90, "High", "2026-09-20 10:00"),
            ("flood", -1.20, 36.91, None, "2026-09-21 09:30"),        # reflex-stage row: severity still NULL
            ("robbery", -1.28, 36.82, "Medium", "2026-09-01 18:00"),
            ("fire", -0.50, 37.00, "Low", "not a timestamp"),          # legacy junk row: falls back to "now"
            ("accident", -1.30, 36.80, "High", "2025-01-01 00:00"),
        ],
    )
    conn.commit()
    conn.close()


def test_point_risk_is_numerically_identical_to_the_original_implementation(client):
    _seed_live_incidents()
    queries = [
        (-1.19, 36.90, "flood"), (-1.28, 36.82, "robbery"), (-1.0, 36.8, "accident"),
        (-0.5, 37.0, "fire"), (-3.0, 39.5, "theft"), (1.5, 38.0, None), (-1.2921, 36.8219, "medical_emergency"),
    ]
    for lat, lon, kind in queries:
        got, _ = rs.point_risk(lat, lon, kind)
        assert abs(got - _reference_point_risk(lat, lon, kind)) < 1e-9, (lat, lon, kind)


def test_load_points_matches_original_arrays(client):
    _seed_live_incidents()
    for category in (None, "crime", "hazard", "medical"):
        assert np.allclose(rs._load_points(category), _reference_load_points(category)), category


def test_csv_cache_invalidates_when_the_file_changes(tmp_path, monkeypatch):
    csv = tmp_path / "synthetic_incidents.csv"
    header = "date,latitude,longitude,severity,category\n"
    csv.write_text(header + "2026-09-01,-1.0,36.8,High,crime\n2026-09-02,-1.1,36.9,Low,hazard\n")
    monkeypatch.setattr(rs, "_CSV_PATH", csv)
    monkeypatch.setitem(rs._csv_cache, "key", None)

    assert len(rs._csv_arrays(None)[0]) == 2
    assert len(rs._csv_arrays("crime")[0]) == 1

    csv.write_text(header + "2026-09-01,-1.0,36.8,High,crime\n2026-09-02,-1.1,36.9,Low,hazard\n2026-09-03,-1.2,37.0,Low,crime\n")
    st = csv.stat()
    os.utime(csv, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))  # guarantee a different mtime
    assert len(rs._csv_arrays(None)[0]) == 3
    assert len(rs._csv_arrays("crime")[0]) == 2
