"""
Seeds synthetic subscriber records for local testing of the broadcast
system. Phone numbers are entirely fake (randomized, no real subscriber
data of any kind) - this is demo data, not a real alert list.
Run: python scripts/seed_subscribers.py
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.database import get_connection, init_db

random.seed(7)

AREAS = {
    "CBD": (-1.2864, 36.8172), "Kibera": (-1.3133, 36.7820), "Kayole": (-1.2750, 36.9220),
    "Karen": (-1.3184, 36.7078), "Westlands": (-1.2685, 36.8110), "Eastleigh": (-1.2790, 36.8530),
}


def fake_phone():
    return f"+2547{random.randint(10000000, 99999999)}"


def main(n=200):
    init_db()
    conn = get_connection()
    for _ in range(n):
        area = random.choice(list(AREAS.keys()))
        lat, lon = AREAS[area]
        conn.execute(
            "INSERT INTO subscribers (phone_number, area, latitude, longitude) VALUES (?, ?, ?, ?)",
            (fake_phone(), area, lat + random.uniform(-0.01, 0.01), lon + random.uniform(-0.01, 0.01)),
        )
    conn.commit()
    conn.close()
    print(f"Seeded {n} synthetic subscribers.")


if __name__ == "__main__":
    main()
