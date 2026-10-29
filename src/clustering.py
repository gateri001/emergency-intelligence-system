"""
Block A: incidents made of reports.

Ten people reporting the same flood are one incident, not ten cards in an
officer's queue. Each report, once the strategic pass has scored it
(src/strategic.py), joins the nearest open incident ("cluster") of the same
type that is close enough in space and time, or starts a new one. This is a
simplified, incremental version of the space-time clustering used in the
literature (ST-DBSCAN), chosen because it is explainable to an officer:
"same kind of thing, within X metres, within Y hours".

What a cluster adds over its reports:
- independent witnesses: two messages from one phone are one witness, not
  two (reporter_key, a keyed hash - never the number itself);
- a lifecycle an officer drives: new -> verified -> assigned -> en route ->
  on scene -> resolved, or dismissed with a reason; every step is logged;
- reporter track records: reports from people whose earlier reports were
  verified start a little more trusted; known pranksters start lower.

Nothing in this module is ever served publicly. The public still only sees
individual reports, filtered by src/privacy.py exactly as before.

Design notes and sources: EIS_TEARDOWN_2026-10-28.md (outside the repo)
and docs/architecture.md, "Block A".
"""
import hashlib
import hmac
from datetime import datetime, timedelta

from src.geo import haversine_km
from src.risk_surface import TYPE_CATEGORY

# (radius km, time window hours) for joining a report to an open incident.
# Judgement calls, not validated: the literature's common default is about
# 300 m / 30 min; floods spread wider and last longer, crimes and medical
# emergencies are point events. Tune these with real reports.
TYPE_RULES = {
    "flood": (1.5, 12.0),
    "fire": (0.5, 6.0),
    "accident": (0.3, 2.0),
    "medical_emergency": (0.2, 2.0),
}
CATEGORY_RULES = {"crime": (0.3, 2.0), "hazard": (1.0, 6.0), "medical": (0.3, 2.0)}
DEFAULT_RULE = (0.5, 4.0)

OPEN_STATUSES = ("new", "verified", "assigned", "en_route", "on_scene")
# Where an officer may move an incident from each status. Nothing leaves
# 'new' except by a human verifying or dismissing it: responders are not
# sent on an unchecked report through this system.
TRANSITIONS = {
    "new": {"verified", "dismissed"},
    "verified": {"assigned", "en_route", "on_scene", "resolved", "dismissed"},
    "assigned": {"en_route", "on_scene", "resolved"},
    "en_route": {"on_scene", "resolved"},
    "on_scene": {"resolved"},
    "resolved": {"new"},    # reopen
    "dismissed": {"new"},   # reopen
    "merged": set(),
}
# Order used when a unit's movement pushes its incident forward.
_PROGRESS = {s: i for i, s in enumerate(("new", "verified", "assigned", "en_route", "on_scene"))}

DISMISS_REASONS = ("prank", "mistaken", "duplicate", "unverifiable", "not_actionable")
UNIT_STATUSES = ("available", "assigned", "en_route", "on_scene", "unavailable")
_TIER_RANK = {"in_app": 0, "sms_recommended": 1, "critical": 2}
TIME_FORMAT = "%Y-%m-%d %H:%M"


class NotFound(LookupError):
    """The thing asked for doesn't exist (the API answers 404)."""


class NotAllowed(ValueError):
    """The request is understood but not allowed in this state (400)."""


def rule_for(incident_type: str) -> tuple[float, float]:
    if incident_type in TYPE_RULES:
        return TYPE_RULES[incident_type]
    return CATEGORY_RULES.get(TYPE_CATEGORY.get(incident_type, ""), DEFAULT_RULE)


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.strptime(ts, TIME_FORMAT)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Reporter pseudonyms and track records
# ---------------------------------------------------------------------------

def reporter_key_for(channel: str, raw_id: str) -> str:
    """A stable pseudonym for a sender: HMAC-SHA256 of their phone number or
    chat id, keyed with EIS_SECRET_KEY. Without the key it can't be turned
    back into the number, even by trying every Kenyan number - so keep that
    key secret and set it explicitly (without it, a fresh random key is used
    per process start and track records reset on every restart)."""
    from src.auth import SECRET_KEY

    digest = hmac.new(SECRET_KEY.encode(), f"{channel}:{raw_id}".encode(), hashlib.sha256).hexdigest()
    return f"{channel}:{digest[:32]}"


def reporter_reputation(conn, reporter_key: str | None, exclude_id: int | None = None) -> float:
    """A confidence adjustment from this sender's earlier reports, as judged
    by officers. Unknown and new senders get 0 (neutral, never trusted by
    default). A report dismissed as a prank costs far more than a verified
    one earns; 'mistaken' and other dismissals cost nothing, so honest
    people who got it wrong aren't punished."""
    if not reporter_key or ":" not in reporter_key or reporter_key.startswith(("officer:", "bulk")):
        return 0.0
    row = conn.execute(
        "SELECT COALESCE(SUM(review_status = 'verified'), 0) AS verified, "
        "COALESCE(SUM(review_status = 'dismissed' AND dismiss_reason = 'prank'), 0) AS pranks "
        "FROM incidents WHERE reporter_key = ? AND id != ?",
        (reporter_key, exclude_id if exclude_id is not None else -1),
    ).fetchone()
    if row["pranks"]:
        return -min(0.15 * row["pranks"], 0.30)
    if row["verified"] >= 3:
        return 0.10
    if row["verified"] >= 1:
        return 0.05
    return 0.0


# ---------------------------------------------------------------------------
# Placing reports into incidents
# ---------------------------------------------------------------------------

def assign_report(conn, incident_id: int) -> int | None:
    """Puts one report into an incident and returns the incident's id. Runs
    inside the caller's transaction (the strategic pass holds BEGIN
    IMMEDIATE), so two reports arriving together can't both start a new
    incident for the same event."""
    row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    if row is None:
        return None
    if row["cluster_id"] is not None:
        return row["cluster_id"]
    if row["latitude"] is None or row["longitude"] is None:
        return None

    radius_km, window_h = rule_for(row["type"])
    when = _parse(row["timestamp"])
    window = timedelta(hours=window_h)
    best_id, best_d = None, None
    placeholders = ", ".join("?" for _ in OPEN_STATUSES)
    for c in conn.execute(
        f"SELECT * FROM clusters WHERE type = ? AND status IN ({placeholders})", (row["type"], *OPEN_STATUSES)
    ).fetchall():
        first, last = _parse(c["first_report_at"]), _parse(c["last_report_at"])
        if when and first and last and (when < first - window or when > last + window):
            continue
        d = haversine_km(row["latitude"], row["longitude"], c["latitude"], c["longitude"])
        if d <= radius_km and (best_d is None or d < best_d):
            best_id, best_d = c["id"], d

    if best_id is None:
        best_id = conn.execute(
            "INSERT INTO clusters (type, latitude, longitude, area, first_report_at, last_report_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (row["type"], row["latitude"], row["longitude"], row["area"] or "", row["timestamp"], row["timestamp"]),
        ).lastrowid
    conn.execute("UPDATE incidents SET cluster_id = ? WHERE id = ?", (best_id, incident_id))
    recompute_cluster(conn, best_id)
    return best_id


def assign_unplaced(conn) -> int:
    """Places every report that has no incident yet, oldest first: reports
    from before Block A existed, and any whose strategic pass failed.
    Returns how many were placed."""
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM incidents WHERE cluster_id IS NULL AND latitude IS NOT NULL ORDER BY id"
    ).fetchall()]
    for incident_id in ids:
        assign_report(conn, incident_id)
    return len(ids)


def forget_orphans(conn) -> int:
    """Deletes incidents that lost all their reports (the satellite fire
    feed replaces its rows on every run) and that no person ever touched.
    Anything an officer acted on stays, with its audit trail, as a record."""
    return conn.execute(
        "DELETE FROM clusters WHERE status = 'new' "
        "AND NOT EXISTS (SELECT 1 FROM incidents i WHERE i.cluster_id = clusters.id) "
        "AND NOT EXISTS (SELECT 1 FROM audit_log a WHERE a.target_type = 'cluster' AND a.target_id = clusters.id)"
    ).rowcount


def recompute_cluster(conn, cluster_id: int) -> None:
    """Refreshes everything about an incident that comes from its reports.
    Reports an officer dismissed stay attached (they're part of the record)
    but add no confidence and no witness."""
    members = conn.execute("SELECT * FROM incidents WHERE cluster_id = ?", (cluster_id,)).fetchall()
    if not members:
        return
    live = [m for m in members if m["review_status"] != "dismissed"]
    keys, anonymous = set(), 0
    for m in live:
        if m["reporter_key"]:
            keys.add(m["reporter_key"])
        else:
            anonymous += 1  # web reports carry no identity: each counts once (scores still cap corroboration)
    confidence = max((m["confidence_score"] for m in live), default=0.0)
    tier = max((m["alert_tier"] for m in live), key=lambda t: _TIER_RANK.get(t, 0), default="in_app")
    area = next((m["area"] for m in sorted(members, key=lambda m: m["id"], reverse=True) if m["area"]), "")
    conn.execute(
        "UPDATE clusters SET latitude = ?, longitude = ?, area = ?, report_count = ?, independent_count = ?, "
        "confidence_score = ?, alert_tier = ?, officers_only = ?, first_report_at = ?, last_report_at = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (
            round(sum(m["latitude"] for m in members) / len(members), 6),
            round(sum(m["longitude"] for m in members) / len(members), 6),
            area, len(members), len(keys) + anonymous, confidence, tier,
            int(any(m["visibility"] == "officers_only" for m in members)),
            min(m["timestamp"] for m in members), max(m["timestamp"] for m in members),
            cluster_id,
        ),
    )


# ---------------------------------------------------------------------------
# Officer actions. Each runs in the caller's transaction and writes its own
# audit entry, so an action and its log are committed together or not at all.
# ---------------------------------------------------------------------------

def _get_cluster(conn, cluster_id: int):
    row = conn.execute("SELECT * FROM clusters WHERE id = ?", (cluster_id,)).fetchone()
    if row is None:
        raise NotFound("Incident not found")
    return row


def _log(conn, actor, action, target_type, target_id, detail=""):
    from src.privacy import log_action

    log_action(conn, actor, action, target_type, target_id, detail)


def release_units(conn, cluster_id: int, actor: str) -> None:
    for u in conn.execute("SELECT id, name FROM units WHERE cluster_id = ?", (cluster_id,)).fetchall():
        conn.execute(
            "UPDATE units SET status = 'available', cluster_id = NULL, updated_by = ?, "
            "updated_at = datetime('now') WHERE id = ?", (actor, u["id"]),
        )
        _log(conn, actor, "unit_released", "cluster", cluster_id, u["name"])


def set_status(conn, cluster_id: int, new_status: str, actor: str, note: str = "",
               reason: str | None = None) -> None:
    c = _get_cluster(conn, cluster_id)
    old = c["status"]
    if new_status not in TRANSITIONS.get(old, set()):
        raise NotAllowed(f"An incident that is '{old}' can't be moved to '{new_status}'")
    if new_status == "dismissed" and reason not in DISMISS_REASONS:
        raise NotAllowed(f"Dismissing needs a reason: {', '.join(DISMISS_REASONS)}")

    if new_status == "verified":
        # The officer's check covers every report not yet judged on its own.
        conn.execute(
            "UPDATE incidents SET review_status = 'verified', reviewed_by = ?, dismiss_reason = NULL "
            "WHERE cluster_id = ? AND review_status IS NULL", (actor, cluster_id),
        )
    elif new_status == "dismissed":
        conn.execute(
            "UPDATE incidents SET review_status = 'dismissed', reviewed_by = ?, dismiss_reason = ? "
            "WHERE cluster_id = ? AND review_status IS NULL", (actor, reason, cluster_id),
        )
    elif new_status == "new" and old == "dismissed":
        # Reopening undoes the dismissal it cascaded (same reason, same
        # incident), so nobody keeps a prank mark from a reversed decision.
        conn.execute(
            "UPDATE incidents SET review_status = NULL, reviewed_by = NULL, dismiss_reason = NULL "
            "WHERE cluster_id = ? AND review_status = 'dismissed' AND dismiss_reason IS ?",
            (cluster_id, c["dismiss_reason"]),
        )
    if new_status in ("resolved", "dismissed"):
        release_units(conn, cluster_id, actor)

    conn.execute(
        "UPDATE clusters SET status = ?, dismiss_reason = ?, updated_at = datetime('now') WHERE id = ?",
        (new_status, reason if new_status == "dismissed" else None, cluster_id),
    )
    detail = " · ".join(x for x in (f"from {old}", reason or "", note) if x)
    _log(conn, actor, f"incident_{new_status}", "cluster", cluster_id, detail)
    recompute_cluster(conn, cluster_id)


def set_flags(conn, cluster_id: int, flags: dict, actor: str) -> None:
    _get_cluster(conn, cluster_id)
    changed = {k: bool(v) for k, v in flags.items() if v is not None}
    if not changed:
        return
    conn.execute(
        "UPDATE clusters SET " + ", ".join(f"{k} = ?" for k in changed) + ", updated_at = datetime('now') WHERE id = ?",
        (*[int(v) for v in changed.values()], cluster_id),
    )
    detail = ", ".join(f"{k.replace('_', ' ')} {'on' if v else 'off'}" for k, v in changed.items())
    _log(conn, actor, "incident_flags", "cluster", cluster_id, detail)


def set_commander(conn, cluster_id: int, commander: str | None, actor: str) -> None:
    _get_cluster(conn, cluster_id)
    if commander is not None and conn.execute(
        "SELECT 1 FROM officers WHERE username = ?", (commander,)
    ).fetchone() is None:
        raise NotAllowed("No account with that username")
    conn.execute("UPDATE clusters SET commander = ?, updated_at = datetime('now') WHERE id = ?",
                 (commander, cluster_id))
    _log(conn, actor, "incident_commander", "cluster", cluster_id, commander or "cleared")


def merge(conn, target_id: int, other_id: int, actor: str) -> None:
    """Folds `other` into `target`: an officer saw two incidents are one."""
    if target_id == other_id:
        raise NotAllowed("An incident can't be merged into itself")
    target, other = _get_cluster(conn, target_id), _get_cluster(conn, other_id)
    if "merged" in (target["status"], other["status"]):
        raise NotAllowed("One of these was already merged into another incident")
    if target["type"] != other["type"]:
        raise NotAllowed("Only incidents of the same type can be merged")
    conn.execute("UPDATE incidents SET cluster_id = ? WHERE cluster_id = ?", (target_id, other_id))
    conn.execute("UPDATE units SET cluster_id = ? WHERE cluster_id = ?", (target_id, other_id))
    conn.execute(
        "UPDATE clusters SET status = 'merged', merged_into = ?, updated_at = datetime('now') WHERE id = ?",
        (target_id, other_id),
    )
    _log(conn, actor, "incident_merge", "cluster", target_id, f"absorbed #{other_id}")
    _log(conn, actor, "incident_merged_into", "cluster", other_id, f"into #{target_id}")
    recompute_cluster(conn, target_id)


def detach(conn, incident_id: int, actor: str) -> int:
    """Takes one report out of its incident into a new one of its own: an
    officer saw the system grouped two different events. Returns the new
    incident's id."""
    row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    if row is None:
        raise NotFound("Report not found")
    old_id = row["cluster_id"]
    if old_id is None:
        raise NotAllowed("This report isn't in an incident yet")
    if conn.execute("SELECT COUNT(*) FROM incidents WHERE cluster_id = ?", (old_id,)).fetchone()[0] < 2:
        raise NotAllowed("This is the incident's only report; there is nothing to split off")
    new_id = conn.execute(
        "INSERT INTO clusters (type, latitude, longitude, area, first_report_at, last_report_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (row["type"], row["latitude"], row["longitude"], row["area"] or "", row["timestamp"], row["timestamp"]),
    ).lastrowid
    conn.execute("UPDATE incidents SET cluster_id = ? WHERE id = ?", (new_id, incident_id))
    recompute_cluster(conn, old_id)
    recompute_cluster(conn, new_id)
    _log(conn, actor, "incident_split", "cluster", old_id, f"report #{incident_id} moved to new incident #{new_id}")
    _log(conn, actor, "incident_split_from", "cluster", new_id, f"from #{old_id}")
    return new_id


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

def set_unit_status(conn, unit_id: int, status: str, cluster_id: int | None, actor: str, note: str = "") -> None:
    unit = conn.execute("SELECT * FROM units WHERE id = ?", (unit_id,)).fetchone()
    if unit is None:
        raise NotFound("Unit not found")
    if status in ("assigned", "en_route", "on_scene"):
        cluster_id = cluster_id if cluster_id is not None else unit["cluster_id"]
        if cluster_id is None:
            raise NotAllowed("Say which incident the unit is going to")
        c = _get_cluster(conn, cluster_id)
        if c["status"] not in ("verified", "assigned", "en_route", "on_scene"):
            raise NotAllowed("Units go only to verified, open incidents. Verify it first.")
        conn.execute(
            "UPDATE units SET status = ?, cluster_id = ?, note = ?, updated_by = ?, updated_at = datetime('now') "
            "WHERE id = ?", (status, cluster_id, note, actor, unit_id),
        )
        _log(conn, actor, f"unit_{status}", "cluster", cluster_id, unit["name"])
        # The incident moves forward with its furthest unit, never backward.
        if _PROGRESS[status] > _PROGRESS[c["status"]]:
            conn.execute("UPDATE clusters SET status = ?, updated_at = datetime('now') WHERE id = ?",
                         (status, cluster_id))
    elif status in ("available", "unavailable"):
        previous = unit["cluster_id"]
        conn.execute(
            "UPDATE units SET status = ?, cluster_id = NULL, note = ?, updated_by = ?, updated_at = datetime('now') "
            "WHERE id = ?", (status, note, actor, unit_id),
        )
        if previous is not None:
            _log(conn, actor, "unit_released", "cluster", previous, unit["name"])
        _log(conn, actor, f"unit_{status}", "unit", unit_id, note)
    else:
        raise NotAllowed(f"Unknown unit status; use one of: {', '.join(UNIT_STATUSES)}")


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------

# Audit entries an officer's timeline may show. Deliberately an allowlist:
# opening a crime reporter's number is logged too, and that log (with its
# reason) stays owner-only - it must never leak into a shared timeline.
_TIMELINE_INCIDENT_ACTIONS = ("verify_incident", "dismiss_incident")


def timeline(conn, cluster_id: int) -> list[dict]:
    """Everything that happened to one incident, oldest first: reports
    arriving, officers' decisions, units moving, alerts sent."""
    members = conn.execute(
        "SELECT id, source, area, created_at FROM incidents WHERE cluster_id = ?", (cluster_id,)
    ).fetchall()
    ids = [m["id"] for m in members]
    events = [
        {"at": m["created_at"], "kind": "report", "actor": None,
         "text": f"Report #{m['id']} from {m['source']}" + (f" · {m['area']}" if m["area"] else "")}
        for m in members
    ]
    for e in conn.execute(
        "SELECT * FROM audit_log WHERE target_type = 'cluster' AND target_id = ?", (cluster_id,)
    ).fetchall():
        events.append({"at": e["created_at"], "kind": "action", "actor": e["actor"],
                       "text": e["action"].replace("_", " ") + (f" · {e['detail']}" if e["detail"] else "")})
    if ids:
        marks = ", ".join("?" for _ in ids)
        for e in conn.execute(
            f"SELECT * FROM audit_log WHERE target_type = 'incident' AND target_id IN ({marks}) "
            f"AND action IN ({', '.join('?' for _ in _TIMELINE_INCIDENT_ACTIONS)})",
            (*ids, *_TIMELINE_INCIDENT_ACTIONS),
        ).fetchall():
            events.append({"at": e["created_at"], "kind": "action", "actor": e["actor"],
                           "text": f"report #{e['target_id']}: " + e["action"].replace("_", " ")})
        for b in conn.execute(
            f"SELECT * FROM broadcasts WHERE incident_id IN ({marks})", ids
        ).fetchall():
            events.append({"at": b["created_at"], "kind": "alert", "actor": b["triggered_by"],
                           "text": f"SMS alert to {b['recipient_count']} people within {b['radius_km']} km"})
    events.sort(key=lambda e: (e["at"] or ""))
    return events
