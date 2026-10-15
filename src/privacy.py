"""
Reporter protection for crime reports, plus the audit log.

A crime witness can be hurt if the person they reported finds out who they
are. Four rules follow from that, and every public route out of the system
applies them through this module:

1. Crime reports are officers-only unless the reporter ticks "also warn
   people nearby" (sent as visibility='public').
2. A public crime report is held back for CRIME_PUBLIC_DELAY_MINUTES after
   it arrives, so a pin doesn't appear while the reporter is still standing
   where they reported from.
3. Publicly, a crime location is snapped to a fixed ~500 m grid. Snapping,
   not random jitter: jitter can be averaged away by reloading many times;
   a fixed grid always gives the same answer.
4. The reporter's phone number (optional, crime only) is stored encrypted
   and no officer endpoint returns it. Only an admin or the owner can open
   it, with a written reason, and every opening is written to the audit log.
"""
import logging
import math
import os

from src.risk_surface import TYPE_CATEGORY

logger = logging.getLogger("privacy")

CRIME_TYPES = tuple(sorted(t for t, c in TYPE_CATEGORY.items() if c == "crime"))
CRIME_PUBLIC_DELAY_MINUTES = 15
# ~500 m in latitude everywhere; in longitude ~500 m at the equator, and Kenya
# straddles it (cos(5 deg) = 0.996), so one step size is fine for both axes.
CRIME_GRID_DEG = 0.0045

# SQL fragment (for incidents rows) meaning "the public may see this row".
# Officers-only rows never; crime rows only once the delay has passed. Rows
# from before created_at existed (NULL) are old, so they count as past it.
_crime_list = ", ".join(f"'{t}'" for t in CRIME_TYPES)
PUBLIC_INCIDENT_SQL = (
    "visibility = 'public' AND (type NOT IN (" + _crime_list + ") "
    "OR created_at IS NULL "
    f"OR created_at <= datetime('now', '-{CRIME_PUBLIC_DELAY_MINUTES} minutes'))"
)


def is_crime(incident_type: str) -> bool:
    return TYPE_CATEGORY.get(incident_type) == "crime"


def snap(value: float) -> float:
    """Centre of the ~500 m grid cell containing `value` (degrees)."""
    return round((math.floor(value / CRIME_GRID_DEG) + 0.5) * CRIME_GRID_DEG, 6)


def public_incident(row: dict) -> dict:
    """The public version of an incident row: crime locations blurred."""
    out = dict(row)
    if is_crime(out.get("type", "")) and out.get("latitude") is not None and out.get("longitude") is not None:
        out["latitude"] = snap(out["latitude"])
        out["longitude"] = snap(out["longitude"])
    return out


# ---------------------------------------------------------------------------
# Encryption of reporter phone numbers (key: EIS_DATA_KEY, a Fernet key).
# If the key is lost, the stored numbers cannot be recovered - by design.
# Keep a copy of the key somewhere offline. Generate one with
# scripts/generate_keys.py.
# ---------------------------------------------------------------------------

def _fernet():
    key = os.environ.get("EIS_DATA_KEY")
    if not key:
        return None
    from cryptography.fernet import Fernet

    try:
        return Fernet(key.encode())
    except ValueError:
        logger.error("EIS_DATA_KEY is not a valid Fernet key; reporter phone numbers will not be stored")
        return None


def encryption_available() -> bool:
    return _fernet() is not None


def encrypt(plaintext: str) -> str | None:
    f = _fernet()
    return f.encrypt(plaintext.encode()).decode() if f else None


def decrypt(token: str) -> str:
    f = _fernet()
    if f is None:
        raise RuntimeError("EIS_DATA_KEY is not set or invalid, so stored numbers cannot be opened")
    return f.decrypt(token.encode()).decode()


# ---------------------------------------------------------------------------
# Audit log: who did what sensitive thing, when, and why.
# ---------------------------------------------------------------------------

def log_action(conn, actor: str, action: str, target_type: str, target_id: int | None, detail: str = "") -> None:
    """Written in the caller's transaction, so the action and its log entry
    are committed together or not at all."""
    conn.execute(
        "INSERT INTO audit_log (actor, action, target_type, target_id, detail) VALUES (?, ?, ?, ?, ?)",
        (actor, action, target_type, target_id, detail),
    )
