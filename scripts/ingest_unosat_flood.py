"""
Pulls UNOSAT's satellite-derived flood mapping for the April 2024 Kenya
floods from the Humanitarian Data Exchange (HDX) - real, ground-truthed
flood extent polygons and individually-identified affected structures,
not estimates.

This is a one-time historical event snapshot (FL20240426KEN), not a live
feed like GDACS - useful as real ground truth to calibrate against, not as
an ongoing data source. Source data is licensed CC-BY-SA, no registration
required.

Requires: pyshp, shapely, requests (see requirements.txt)
Run: python scripts/ingest_unosat_flood.py
"""
import io
import json
import sys
import zipfile
from pathlib import Path

import requests
import shapefile
from shapely.geometry import MultiPolygon, Point, Polygon, mapping, shape

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.database import get_connection, init_db

HDX_PACKAGE_URL = "https://data.humdata.org/api/3/action/package_show?id=unosat-live-web-map-flood-in-kenya"

# Same named areas used for synthetic data / risk grid, so affected-structure
# counts can be directly compared against them.
KNOWN_AREAS = {
    "CBD": (-1.2864, 36.8172), "Kibera": (-1.3133, 36.7820), "Kayole": (-1.2750, 36.9220),
    "Karen": (-1.3184, 36.7078), "Westlands": (-1.2685, 36.8110), "Eastleigh": (-1.2790, 36.8530),
    "Kasarani": (-1.2280, 36.9000), "Langata": (-1.3450, 36.7620), "Umoja": (-1.2860, 36.8960),
    "Donholm": (-1.2990, 36.8880), "Embakasi": (-1.3180, 36.8940), "Ruiru": (-1.1490, 36.9570),
    "Thika": (-1.0400, 37.0890), "Ngong": (-1.3600, 36.6550), "Kawangware": (-1.2830, 36.7500),
    "Githurai": (-1.2060, 36.9040),
}


def _nearest_area(lat, lon):
    best, best_dist = None, float("inf")
    for name, (alat, alon) in KNOWN_AREAS.items():
        d = ((lat - alat) ** 2 + (lon - alon) ** 2) ** 0.5
        if d < best_dist:
            best, best_dist = name, d
    return best, best_dist


def _find_resource_url(name_substring: str) -> str:
    resp = requests.get(HDX_PACKAGE_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    resp.raise_for_status()
    resources = resp.json()["result"]["resources"]
    for r in resources:
        if name_substring in r["name"] and r["format"] == "SHP":
            return r["url"]
    raise RuntimeError(f"No SHP resource matching {name_substring!r} found")


def _download_and_extract_shp(url: str, extract_dir: Path):
    print(f"Downloading {url} ...")
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=180)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        zf.extractall(extract_dir)
    print(f"Extracted to {extract_dir}")


def main():
    init_db()  # schema for flood_extents / affected_structures_summary lives centrally in src/database.py
    conn = get_connection()

    cache_dir = Path(__file__).resolve().parent.parent / ".cache" / "unosat"
    cache_dir.mkdir(parents=True, exist_ok=True)

    shp_dir = cache_dir / "FL20240426KEN_SHP"
    if not shp_dir.exists():
        url = _find_resource_url("FL20240426KEN_SHP")
        _download_and_extract_shp(url, cache_dir)

    base = shp_dir

    # --- Flood extent polygon (Nairobi/Kiambu) - simplified for web display ---
    # Raw source is ~1.1M points across 953 fragments; the dominant fragment
    # alone carries 6,582 interior holes (small unflooded pockets within the
    # flood zone - individual buildings/high ground per the satellite
    # classification). For a city-scale risk map those holes are noise, not
    # signal, so: drop interior rings, keep the fragments that make up 99% of
    # the mapped flood area, then simplify what's left.
    flood_shp = base / "PL_20240501_FloodExtent_Nairobi_Kiambu"
    sf = shapefile.Reader(str(flood_shp))
    geom = shape(sf.shape(0).__geo_interface__)

    polys_by_area = sorted(geom.geoms, key=lambda p: -p.area)
    total_area = sum(p.area for p in polys_by_area)
    cumulative, keep_n = 0.0, len(polys_by_area)
    for i, p in enumerate(polys_by_area):
        cumulative += p.area
        if cumulative >= 0.99 * total_area:
            keep_n = i + 1
            break

    solid_polys = [Polygon(p.exterior) for p in polys_by_area[:keep_n]]  # holes stripped
    simplified = MultiPolygon(solid_polys).simplify(0.0005, preserve_topology=True)
    geojson_str = json.dumps(mapping(simplified))

    conn.execute(
        "INSERT OR REPLACE INTO flood_extents (event_code, region, geojson, source_date) VALUES (?, ?, ?, ?)",
        ("FL20240426KEN", "Nairobi_Kiambu", geojson_str, "2024-05-01"),
    )
    print(
        f"Flood extent: kept {keep_n}/{len(polys_by_area)} fragments (99% of mapped area), "
        f"{len(geojson_str) / 1024:.0f} KB of GeoJSON stored (simplified from ~1.1M source points, holes dropped)"
    )

    # --- Affected structures - aggregated by nearest known area, not stored individually ---
    struct_shp = base / "PL_20240501_AffectedStructureNairobi_Kiambu"
    sf = shapefile.Reader(str(struct_shp))
    counts = {name: 0 for name in KNOWN_AREAS}
    unmatched = 0
    for shp_rec in sf.shapes():
        lon, lat = shp_rec.points[0]
        area, dist = _nearest_area(lat, lon)
        if dist <= 0.05:  # ~5.5km - only count structures reasonably near a known area
            counts[area] += 1
        else:
            unmatched += 1

    for area, count in counts.items():
        if count > 0:
            conn.execute(
                "INSERT OR REPLACE INTO affected_structures_summary (event_code, area, structure_count) "
                "VALUES (?, ?, ?)",
                ("FL20240426KEN", area, count),
            )

    conn.commit()
    conn.close()

    print(f"Affected structures by area (April 2024 flood, {len(sf)} total, {unmatched} unmatched to a known area):")
    for area, count in sorted(counts.items(), key=lambda x: -x[1]):
        if count > 0:
            print(f"  {area}: {count}")


if __name__ == "__main__":
    main()
