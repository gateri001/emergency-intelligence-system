import os
import sqlite3
from pathlib import Path

# EIS_DB_PATH lets a deployment keep the database on a persistent disk (and
# lets a demo run on a copy). Default: eis.db next to the code.
DB_PATH = Path(os.environ.get("EIS_DB_PATH") or Path(__file__).resolve().parent.parent / "eis.db")


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
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            visibility TEXT NOT NULL DEFAULT 'public' CHECK(visibility IN ('public', 'officers_only')),
            has_evidence INTEGER NOT NULL DEFAULT 0,
            confidence_score REAL NOT NULL DEFAULT 0.0,
            corroboration_count INTEGER NOT NULL DEFAULT 0,
            alert_tier TEXT NOT NULL DEFAULT 'in_app',
            scoring_stage TEXT NOT NULL DEFAULT 'reflex' CHECK(scoring_stage IN ('reflex', 'strategic', 'failed')),
            magnitude REAL,
            base_confidence REAL,
            vote_confirms INTEGER NOT NULL DEFAULT 0,
            vote_disputes INTEGER NOT NULL DEFAULT 0,
            reporter_phone_enc TEXT,
            review_status TEXT CHECK(review_status IN ('verified', 'dismissed')),
            reviewed_by TEXT
        )
    """)
    # Migration for databases created before visibility/confidence scoring
    # existed - ALTER TABLE has no "IF NOT EXISTS" in sqlite, so try each and
    # ignore "duplicate column" if it's already there.
    for col_def in (
        "visibility TEXT NOT NULL DEFAULT 'public'",
        "has_evidence INTEGER NOT NULL DEFAULT 0",
        "confidence_score REAL NOT NULL DEFAULT 0.0",
        "corroboration_count INTEGER NOT NULL DEFAULT 0",
        "alert_tier TEXT NOT NULL DEFAULT 'in_app'",
        "scoring_stage TEXT NOT NULL DEFAULT 'reflex'",
        "magnitude REAL",  # sensor magnitude, e.g. fire radiative power (MW) for satellite fires
        # Confidence before crowd votes. Stored separately so the strategic
        # pass and each vote recompute from it instead of overwriting each other.
        "base_confidence REAL",
        "vote_confirms INTEGER NOT NULL DEFAULT 0",
        "vote_disputes INTEGER NOT NULL DEFAULT 0",
        # Crime reporter's optional phone, encrypted (src/privacy.py). Never
        # returned by any officer endpoint; see the reveal endpoint in main.py.
        "reporter_phone_enc TEXT",
        # An officer's call on a report: 'verified' (checked, real) or
        # 'dismissed' (not real / not actionable). NULL = not reviewed yet.
        "review_status TEXT CHECK(review_status IN ('verified', 'dismissed'))",
        "reviewed_by TEXT",
        # Block A: which incident (cluster) this report belongs to - see
        # src/clustering.py. NULL until the strategic pass has placed it.
        "cluster_id INTEGER",
        # A pseudonym for whoever sent the report (keyed hash of their phone
        # or chat id; 'officer:<name>' for officers), so two messages from
        # one person never count as two independent witnesses, and so a
        # reporter's track record can count. NULL = unknown sender (web app).
        "reporter_key TEXT",
        # Why an officer dismissed it: prank / mistaken / duplicate /
        # unverifiable / not_actionable. Only 'prank' lowers reputation.
        "dismiss_reason TEXT",
    ):
        try:
            conn.execute(f"ALTER TABLE incidents ADD COLUMN {col_def}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
    conn.execute("CREATE INDEX IF NOT EXISTS idx_incidents_cluster ON incidents(cluster_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_incidents_reporter ON incidents(reporter_key)")
    # Block A: an incident as responders see it - one real-world event, made
    # of one or more reports (src/clustering.py). Officers only; nothing here
    # is ever served publicly. Most fields are recomputed from the member
    # reports; status, commander and the flags are officers' decisions.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clusters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new' CHECK(status IN
                ('new', 'verified', 'assigned', 'en_route', 'on_scene', 'resolved', 'dismissed', 'merged')),
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            area TEXT NOT NULL DEFAULT '',
            report_count INTEGER NOT NULL DEFAULT 0,
            independent_count INTEGER NOT NULL DEFAULT 0,
            confidence_score REAL NOT NULL DEFAULT 0.0,
            alert_tier TEXT NOT NULL DEFAULT 'in_app',
            officers_only INTEGER NOT NULL DEFAULT 0,
            first_report_at TEXT NOT NULL,
            last_report_at TEXT NOT NULL,
            commander TEXT,
            needs_location INTEGER NOT NULL DEFAULT 0,
            needs_translation INTEGER NOT NULL DEFAULT 0,
            escalate INTEGER NOT NULL DEFAULT 0,
            dismiss_reason TEXT,
            merged_into INTEGER REFERENCES clusters(id),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clusters_open ON clusters(type, status)")
    # Block A: responders and their state, for the console's unit board.
    # A unit is anything that can be sent: an engine, an ambulance, a Red
    # Cross team, a community response team.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS units (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            kind TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'available' CHECK(status IN
                ('available', 'assigned', 'en_route', 'on_scene', 'unavailable')),
            cluster_id INTEGER REFERENCES clusters(id),
            note TEXT NOT NULL DEFAULT '',
            updated_by TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS officers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            hashed_password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'officer' CHECK(role IN ('officer', 'admin', 'owner'))
        )
    """)
    # Roles: officer (reviews, verifies, proposes), admin (trusted people:
    # approves missing-child alerts, can open a crime reporter's number with
    # a reason), owner (the founder: everything, and may send alone).
    try:
        conn.execute(
            "ALTER TABLE officers ADD COLUMN role TEXT NOT NULL DEFAULT 'officer' "
            "CHECK(role IN ('officer', 'admin', 'owner'))"
        )
    except sqlite3.OperationalError as e:
        if 'duplicate column' not in str(e).lower():
            raise
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id INTEGER,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
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
            failed_count INTEGER NOT NULL DEFAULT 0,
            triggered_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    try:
        conn.execute("ALTER TABLE broadcasts ADD COLUMN failed_count INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError as e:
        if 'duplicate column' not in str(e).lower():
            raise
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
    # Missing Child Alert - deliberately its own table, not a fifth
    # `incidents` type: different fields, different lifecycle (doesn't
    # decay, stays active until resolved), different trust model (starts
    # hidden and requires officer verification before any public
    # visibility - the opposite default from flood/fire, given the real
    # risk of a weaponized report - custody disputes, harassment - and the
    # legal weight of a minor's data under Kenya's Data Protection Act).
    # reporter_phone is a deliberate, singular exception to "no identity is
    # ever collected" elsewhere in this system: this is fundamentally an
    # investigation, and officers need to be able to follow up with
    # whoever reported it. Never exposed publicly - see /missing-child/cases
    # vs /missing-child/cases/all in src/main.py.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS missing_child_cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            child_name TEXT NOT NULL,
            age INTEGER,
            physical_description TEXT NOT NULL,
            clothing_description TEXT NOT NULL DEFAULT '',
            last_seen_latitude REAL NOT NULL,
            last_seen_longitude REAL NOT NULL,
            last_seen_area TEXT NOT NULL DEFAULT '',
            last_seen_time TEXT NOT NULL,
            has_photo INTEGER NOT NULL DEFAULT 0,
            reporter_phone TEXT NOT NULL,
            reporter_relationship TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'reported'
                CHECK(status IN ('reported', 'verified', 'found_safe', 'found_deceased', 'closed_false_report')),
            verified_by TEXT,
            resolved_by TEXT,
            ob_number TEXT,
            expires_at TEXT,
            followup_sent_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    for col_def in (
        "resolved_by TEXT",
        "ob_number TEXT",         # police Occurrence Book number, required to verify
        "expires_at TEXT",        # a verified case leaves public view at this time unless renewed
        "followup_sent_at TEXT",  # when the "alert closed" SMS went out
    ):
        try:
            conn.execute(f"ALTER TABLE missing_child_cases ADD COLUMN {col_def}")
        except sqlite3.OperationalError as e:
            if 'duplicate column' not in str(e).lower():
                raise
    # Broadcast log for Missing Child Alert. Its own table (not `broadcasts`)
    # because `broadcasts.incident_id` is NOT NULL and references incidents,
    # which SQLite can't relax in place, and a case isn't an incident.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS missing_child_broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER NOT NULL REFERENCES missing_child_cases(id),
            message TEXT NOT NULL,
            radius_km REAL NOT NULL,
            recipient_count INTEGER NOT NULL,
            failed_count INTEGER NOT NULL DEFAULT 0,
            triggered_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    try:
        conn.execute("ALTER TABLE missing_child_broadcasts ADD COLUMN approved_by TEXT")
    except sqlite3.OperationalError as e:
        if 'duplicate column' not in str(e).lower():
            raise
    # Who each missing-child alert actually reached, so the "alert closed"
    # follow-up goes to exactly those people and nobody else.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS missing_child_broadcast_recipients (
            broadcast_id INTEGER NOT NULL REFERENCES missing_child_broadcasts(id),
            phone_number TEXT NOT NULL
        )
    """)
    # Two-person rule: an officer or admin proposes the exact message and
    # radius; a different admin, or the owner, approves it and it is sent.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS missing_child_broadcast_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER NOT NULL REFERENCES missing_child_cases(id),
            message TEXT NOT NULL,
            radius_km REAL NOT NULL,
            proposed_by TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected')),
            decided_by TEXT,
            broadcast_id INTEGER REFERENCES missing_child_broadcasts(id),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            decided_at TEXT
        )
    """)
    # Populated by scripts/ingest_health_facilities.py - real hospital/clinic
    # locations (healthsites.io via HDX), for "route to help" (medical
    # incidents) rather than "route away from danger" (the risk-avoidance
    # safe route). Unlike FIRMS, this is a slowly-changing registry, not a
    # rolling snapshot - re-running accumulates/updates by osm_id rather
    # than deleting and replacing.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS health_facilities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            osm_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            amenity TEXT NOT NULL,
            has_emergency TEXT,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()
