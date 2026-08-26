"""
Generates a synthetic incident dataset for demo/training purposes.
No real personal data of any kind - locations are generalized to named
areas (not exact addresses), and no names/IDs/phone numbers are generated.
Run: python scripts/generate_synthetic_data.py
"""
import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

random.seed(42)

AREAS = {
    "CBD": (-1.2864, 36.8172), "Kibera": (-1.3133, 36.7820), "Kayole": (-1.2750, 36.9220),
    "Karen": (-1.3184, 36.7078), "Westlands": (-1.2685, 36.8110), "Eastleigh": (-1.2790, 36.8530),
    "Kasarani": (-1.2280, 36.9000), "Langata": (-1.3450, 36.7620), "Umoja": (-1.2860, 36.8960),
    "Donholm": (-1.2990, 36.8880), "Embakasi": (-1.3180, 36.8940), "Ruiru": (-1.1490, 36.9570),
    "Thika": (-1.0400, 37.0890), "Ngong": (-1.3600, 36.6550), "Kawangware": (-1.2830, 36.7500),
    "Githurai": (-1.2060, 36.9040),
}

TYPES = {
    "burglary": "crime", "robbery": "crime", "theft": "crime", "assault": "crime",
    "vandalism": "crime", "suspicious_activity": "crime",
    "flood": "hazard", "fire": "hazard",
    "accident": "medical", "medical_emergency": "medical",
}

SEVERITY_WEIGHTS = {"Low": 0.5, "Medium": 0.35, "High": 0.15}

# Real flood-risk weighting per area, from UNOSAT's satellite-verified count of
# flood-damaged structures in the April 2024 Kenya floods (see
# scripts/ingest_unosat_flood.py). Areas with no confirmed structures in that
# event get a small baseline weight, not zero - a single historical event is
# real evidence, not proof an area can never flood.
FLOOD_AREA_WEIGHTS = {
    "Githurai": 2201, "Kayole": 1354, "Donholm": 979, "Kasarani": 645,
    "Eastleigh": 476, "Ruiru": 470, "Umoja": 303,
}
FLOOD_BASELINE_WEIGHT = 20  # for areas with no confirmed damage in that event


def jitter(coord, spread=0.01):
    return coord + random.uniform(-spread, spread)


def weighted_choice(weights: dict):
    return random.choices(list(weights.keys()), weights=list(weights.values()), k=1)[0]


def pick_area_for(inc_type: str) -> str:
    if inc_type != "flood":
        return random.choice(list(AREAS.keys()))
    weights = {a: FLOOD_AREA_WEIGHTS.get(a, FLOOD_BASELINE_WEIGHT) for a in AREAS}
    return weighted_choice(weights)


def generate(n=1000):
    rows = []
    start = datetime(2026, 1, 1)
    for i in range(1, n + 1):
        inc_type = random.choice(list(TYPES.keys()))
        area = pick_area_for(inc_type)
        lat, lon = AREAS[area]
        dt = start + timedelta(days=random.randint(0, 240), hours=random.randint(0, 23),
                                minutes=random.randint(0, 59))
        rows.append({
            "incident_id": f"INC{i:05d}",
            "type": inc_type,
            "category": TYPES[inc_type],
            "area": area,
            "latitude": round(jitter(lat), 6),
            "longitude": round(jitter(lon), 6),
            "date": dt.strftime("%Y-%m-%d"),
            "time": dt.strftime("%H:%M"),
            "severity": weighted_choice(SEVERITY_WEIGHTS),
        })
    return rows


def main():
    rows = generate(1000)
    out_dir = Path(__file__).resolve().parent.parent / "data"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "synthetic_incidents.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} synthetic records to {out_path}")


if __name__ == "__main__":
    main()
