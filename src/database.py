import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "eis.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL CHECK(source IN ('citizen', 'officer', 'bulk')),
            type TEXT NOT NULL,
            area TEXT NOT NULL,
            latitude REAL,
            longitude REAL,
            description TEXT,
            predicted_severity TEXT,
            timestamp TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS officers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            hashed_password TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT NOT NULL,
            area TEXT,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id INTEGER NOT NULL REFERENCES incidents(id),
            message TEXT NOT NULL,
            radius_km REAL NOT NULL,
            recipient_count INTEGER NOT NULL,
            triggered_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # Populated by scripts/ingest_gdacs.py - defined here too so the API
    # never 500s on these tables just because ingestion hasn't run yet.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS external_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            event_type TEXT NOT NULL,
            country TEXT,
            name TEXT,
            latitude REAL,
            longitude REAL,
            from_date TEXT,
            to_date TEXT,
            alert_level TEXT,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(source, event_type, name, from_date)
        )
    """)
    # Populated by scripts/ingest_unosat_flood.py - same reasoning.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS flood_extents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_code TEXT NOT NULL,
            region TEXT NOT NULL,
            geojson TEXT NOT NULL,
            source_date TEXT NOT NULL,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(event_code, region)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS affected_structures_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_code TEXT NOT NULL,
            area TEXT NOT NULL,
            structure_count INTEGER NOT NULL,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(event_code, area)
        )
    """)
    conn.commit()
    conn.close()
