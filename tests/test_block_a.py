"""
Block A: reports grouped into incidents, the incident lifecycle, reporter
track records, and the unit board (src/clustering.py).
"""
from test_api import _report


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def _cluster_of(incident_id):
    from src.database import get_connection

    conn = get_connection()
    row = conn.execute("SELECT cluster_id FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    conn.close()
    return row["cluster_id"]


def _detail(client, token, cluster_id):
    res = client.get(f"/clusters/{cluster_id}", headers=_h(token))
    assert res.status_code == 200, res.text
    return res.json()


def _status(client, token, cluster_id, status, **extra):
    return client.post(f"/clusters/{cluster_id}/status", json={"status": status, **extra}, headers=_h(token))


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def test_reports_of_one_event_become_one_incident(client, officer_token):
    a = _report(client, type="flood", latitude=-1.2600, longitude=36.8600)
    b = _report(client, type="flood", latitude=-1.2610, longitude=36.8610)   # ~150 m away
    c = _report(client, type="flood", latitude=-1.2590, longitude=36.8590, timestamp="2026-09-12 13:00")
    far = _report(client, type="flood", latitude=-1.3200, longitude=36.7800)   # ~11 km away
    fire_here = _report(client, type="fire", latitude=-1.2600, longitude=36.8600)

    assert _cluster_of(a) == _cluster_of(b) == _cluster_of(c)
    assert len({_cluster_of(a), _cluster_of(far), _cluster_of(fire_here)}) == 3

    incident = _detail(client, officer_token, _cluster_of(a))["incident"]
    assert incident["report_count"] == 3 and incident["status"] == "new" and incident["type"] == "flood"


def test_the_same_place_days_later_is_a_new_incident(client):
    a = _report(client, type="fire", latitude=-1.26, longitude=36.86, timestamp="2026-09-12 10:00")
    b = _report(client, type="fire", latitude=-1.26, longitude=36.86, timestamp="2026-09-14 10:00")
    assert _cluster_of(a) != _cluster_of(b)


def test_one_sender_is_one_witness(client, officer_token):
    """Two messages from one phone are one witness, and the second doesn't
    corroborate the first; a different phone does."""
    spot = dict(type="robbery", latitude=-1.2800, longitude=36.8200)
    first = _report(client, reporter_phone="0711000001", **spot)
    again = _report(client, reporter_phone="0711000001", **spot)
    cid = _cluster_of(first)
    assert _cluster_of(again) == cid
    detail = _detail(client, officer_token, cid)
    assert detail["incident"]["report_count"] == 2 and detail["incident"]["independent_count"] == 1
    same_sender = {r["id"]: r["confidence_score"] for r in detail["reports"]}
    assert same_sender[again] == same_sender[first]   # no self-corroboration

    other = _report(client, reporter_phone="0722000002", **spot)
    detail = _detail(client, officer_token, cid)
    assert detail["incident"]["independent_count"] == 2
    scores = {r["id"]: r["confidence_score"] for r in detail["reports"]}
    assert scores[other] > scores[first]


def test_reporter_pseudonym_is_never_served(client, officer_token):
    rid = _report(client, type="robbery", latitude=-1.28, longitude=36.82, reporter_phone="0711000001")
    for res in (client.get("/incidents/all", headers=_h(officer_token)),
                client.get(f"/clusters/{_cluster_of(rid)}", headers=_h(officer_token))):
        assert "reporter_key" not in res.text and "0711000001" not in res.text


def test_public_feed_knows_nothing_about_incidents(client):
    _report(client, type="flood")
    for item in client.get("/incidents").json():
        assert "cluster_id" not in item and "dismiss_reason" not in item
    assert client.get("/clusters").status_code == 401
    assert client.get("/units").status_code == 401


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_nothing_moves_until_a_person_verifies_it(client, officer_token, owner_token):
    a = _report(client, type="flood", latitude=-1.26, longitude=36.86)
    b = _report(client, type="flood", latitude=-1.2605, longitude=36.8605)
    cid = _cluster_of(a)

    assert _status(client, officer_token, cid, "assigned").status_code == 400
    res = _status(client, officer_token, cid, "verified", note="Called the chief")
    assert res.status_code == 200 and res.json()["status"] == "verified"
    reports = {r["id"]: r for r in _detail(client, officer_token, cid)["reports"]}
    assert reports[a]["review_status"] == reports[b]["review_status"] == "verified"

    assert _status(client, officer_token, cid, "resolved").status_code == 200
    assert _detail(client, officer_token, cid)["allowed_next"] == ["new"]
    log = client.get("/audit-log", headers=_h(owner_token)).json()
    assert {"incident_verified", "incident_resolved"} <= {e["action"] for e in log}


def test_dismissing_needs_a_reason_and_reopening_undoes_it(client, officer_token):
    a = _report(client, type="flood", latitude=-1.26, longitude=36.86)
    cid = _cluster_of(a)
    assert _status(client, officer_token, cid, "dismissed").status_code == 400
    assert _status(client, officer_token, cid, "dismissed", reason="prank").status_code == 200
    report = _detail(client, officer_token, cid)["reports"][0]
    assert report["review_status"] == "dismissed" and report["dismiss_reason"] == "prank"

    assert _status(client, officer_token, cid, "new").status_code == 200
    report = _detail(client, officer_token, cid)["reports"][0]
    assert report["review_status"] is None and report["dismiss_reason"] is None


def test_closed_incidents_take_no_new_reports(client):
    a = _report(client, type="fire", latitude=-1.26, longitude=36.86)
    from src.database import get_connection

    conn = get_connection()
    conn.execute("UPDATE clusters SET status = 'resolved' WHERE id = ?", (_cluster_of(a),))
    conn.commit()
    conn.close()
    b = _report(client, type="fire", latitude=-1.26, longitude=36.86)
    assert _cluster_of(b) != _cluster_of(a)


# ---------------------------------------------------------------------------
# Reporter track records
# ---------------------------------------------------------------------------

def test_a_proven_prankster_starts_lower_an_honest_mistake_costs_nothing(client, officer_token):
    def crime(phone, lat):
        return _report(client, type="robbery", latitude=lat, longitude=36.82, reporter_phone=phone)

    def confidence(rid):
        reports = _detail(client, officer_token, _cluster_of(rid))["reports"]
        return next(r["confidence_score"] for r in reports if r["id"] == rid)

    prank = crime("0733000003", -1.20)
    client.post(f"/incidents/{prank}/review", json={"action": "dismiss", "reason": "prank"}, headers=_h(officer_token))
    mistake = crime("0744000004", -1.22)
    client.post(f"/incidents/{mistake}/review", json={"action": "dismiss", "reason": "mistaken"},
                headers=_h(officer_token))

    fresh = confidence(crime("0755000005", -1.30))
    assert confidence(crime("0733000003", -1.34)) < fresh
    assert confidence(crime("0744000004", -1.38)) == fresh


def test_verified_reports_build_trust(client, officer_token):
    phone = "0766000006"
    for i in range(3):
        rid = _report(client, type="flood", latitude=-1.10 - i * 0.1, longitude=36.70, reporter_phone=None)
        assert rid
    # Track records come from phone-keyed senders; floods carry no phone on
    # the web app, so build the record through the reporter_key column the
    # messaging channels (Block B) will set.
    from src.clustering import reporter_key_for, reporter_reputation
    from src.database import get_connection

    key = reporter_key_for("phone", phone)
    conn = get_connection()
    assert reporter_reputation(conn, key) == 0.0
    conn.execute("UPDATE incidents SET reporter_key = ?, review_status = 'verified'", (key,))
    conn.commit()
    assert reporter_reputation(conn, key) == 0.10
    assert reporter_reputation(conn, "officer:test_officer") == 0.0
    conn.close()


# ---------------------------------------------------------------------------
# Merge and split
# ---------------------------------------------------------------------------

def test_merge_and_split(client, officer_token):
    a = _report(client, type="flood", latitude=-1.26, longitude=36.86)
    b = _report(client, type="flood", latitude=-1.30, longitude=36.86)   # ~4.5 km: separate
    fire = _report(client, type="fire", latitude=-1.26, longitude=36.86)
    ca, cb = _cluster_of(a), _cluster_of(b)
    assert ca != cb

    assert client.post(f"/clusters/{ca}/merge", json={"other_id": _cluster_of(fire)},
                       headers=_h(officer_token)).status_code == 400
    merged = client.post(f"/clusters/{ca}/merge", json={"other_id": cb}, headers=_h(officer_token))
    assert merged.status_code == 200 and merged.json()["report_count"] == 2
    assert _cluster_of(b) == ca
    assert _detail(client, officer_token, cb)["incident"]["merged_into"] == ca
    assert cb not in [c["id"] for c in client.get("/clusters?state=all", headers=_h(officer_token)).json()]

    split = client.post(f"/incidents/{b}/detach", headers=_h(officer_token))
    assert split.status_code == 200 and _cluster_of(b) == split.json()["id"] != ca
    assert client.post(f"/incidents/{b}/detach", headers=_h(officer_token)).status_code == 400


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

def test_units_follow_and_move_their_incident(client, officer_token, admin_token):
    assert client.post("/units", json={"name": "Engine 1"}, headers=_h(officer_token)).status_code == 403
    unit = client.post("/units", json={"name": "Engine 1", "kind": "fire engine"}, headers=_h(admin_token)).json()
    assert client.post("/units", json={"name": "Engine 1"}, headers=_h(admin_token)).status_code == 400

    rid = _report(client, type="fire", latitude=-1.26, longitude=36.86)
    cid = _cluster_of(rid)

    def move(status, **extra):
        return client.post(f"/units/{unit['id']}/status", json={"status": status, **extra}, headers=_h(officer_token))

    assert move("assigned", cluster_id=cid).status_code == 400          # not verified yet
    _status(client, officer_token, cid, "verified")
    assert move("assigned", cluster_id=cid).json()["cluster_id"] == cid
    assert _detail(client, officer_token, cid)["incident"]["status"] == "assigned"
    assert move("on_scene").status_code == 200                          # stays on its incident
    detail = _detail(client, officer_token, cid)
    assert detail["incident"]["status"] == "on_scene" and detail["incident"]["units_count"] == 1
    assert [u["name"] for u in detail["units"]] == ["Engine 1"]

    _status(client, officer_token, cid, "resolved")
    freed = client.get("/units", headers=_h(officer_token)).json()[0]
    assert freed["status"] == "available" and freed["cluster_id"] is None


# ---------------------------------------------------------------------------
# Timeline, commander, flags
# ---------------------------------------------------------------------------

def test_timeline_shows_the_story_but_never_a_number_opening(client, officer_token, admin_token, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("EIS_DATA_KEY", Fernet.generate_key().decode())
    rid = _report(client, type="robbery", latitude=-1.28, longitude=36.82, reporter_phone="0711000001")
    cid = _cluster_of(rid)
    opened = client.post(f"/incidents/{rid}/reveal-reporter",
                         json={"reason": "Investigating officer needs a witness statement"},
                         headers=_h(admin_token))
    assert opened.status_code == 200
    _status(client, officer_token, cid, "verified", note="Patrol confirmed")
    assert client.post(f"/clusters/{cid}/commander", json={"commander": "test_officer"},
                       headers=_h(officer_token)).json()["commander"] == "test_officer"
    assert client.post(f"/clusters/{cid}/commander", json={"commander": "nobody_here"},
                       headers=_h(officer_token)).status_code == 400
    flagged = client.post(f"/clusters/{cid}/flags", json={"escalate": True}, headers=_h(officer_token)).json()
    assert flagged["escalate"] is True and flagged["needs_location"] is False

    events = _detail(client, officer_token, cid)["timeline"]
    text = " | ".join(e["text"] for e in events)
    assert events[0]["kind"] == "report"
    assert "incident verified" in text and "Patrol confirmed" in text and "incident commander" in text
    assert "reveal" not in text and "witness statement" not in text


def test_escalated_incidents_lead_the_list(client, officer_token):
    calm = _cluster_of(_report(client, type="flood", latitude=-1.26, longitude=36.86))
    hot = _cluster_of(_report(client, type="flood", latitude=-1.40, longitude=36.70))
    client.post(f"/clusters/{hot}/flags", json={"escalate": True}, headers=_h(officer_token))
    ids = [c["id"] for c in client.get("/clusters", headers=_h(officer_token)).json()]
    assert ids.index(hot) < ids.index(calm)


def test_a_live_incident_outranks_an_old_critical_one(client, officer_token):
    """Found on the demo data: a week of old satellite fires (all 'critical')
    buried tonight's flood. Recent activity now ranks above tier."""
    from src.database import get_connection

    old = _cluster_of(_report(client, type="fire", latitude=-1.40, longitude=36.70))
    live = _cluster_of(_report(client, type="flood", latitude=-1.26, longitude=36.86))
    conn = get_connection()
    conn.execute("UPDATE incidents SET created_at = datetime('now', '-3 days') WHERE cluster_id = ?", (old,))
    conn.execute("UPDATE clusters SET alert_tier = 'critical' WHERE id = ?", (old,))
    conn.execute("UPDATE clusters SET alert_tier = 'in_app' WHERE id = ?", (live,))
    conn.commit()
    conn.close()
    listed = client.get("/clusters", headers=_h(officer_token)).json()
    ids = [c["id"] for c in listed]
    assert ids.index(live) < ids.index(old)
    assert next(c for c in listed if c["id"] == old)["last_activity_at"] is not None


# ---------------------------------------------------------------------------
# Old data and the satellite feed
# ---------------------------------------------------------------------------

def test_old_reports_are_placed_and_empty_untouched_incidents_are_forgotten(client, officer_token):
    from src.clustering import assign_unplaced, forget_orphans
    from src.database import get_connection

    conn = get_connection()
    for lat in (-1.2600, -1.2605):
        conn.execute(
            "INSERT INTO incidents (source, type, area, latitude, longitude, description, timestamp, "
            "confidence_score, alert_tier, scoring_stage) "
            "VALUES ('bulk', 'fire', '', ?, 36.86, 'satellite', '2026-09-12 10:00', 0.8, 'critical', 'strategic')",
            (lat,),
        )
    conn.commit()
    assert assign_unplaced(conn) == 2
    conn.commit()
    cid = conn.execute("SELECT cluster_id FROM incidents ORDER BY id DESC").fetchone()["cluster_id"]
    assert conn.execute("SELECT report_count FROM clusters WHERE id = ?", (cid,)).fetchone()[0] == 2

    # The satellite feed replaces its rows: the untouched incident goes...
    conn.execute("DELETE FROM incidents WHERE source = 'bulk'")
    assert forget_orphans(conn) == 1
    conn.commit()
    conn.close()
    assert client.get(f"/clusters/{cid}", headers=_h(officer_token)).status_code == 404


def test_an_incident_someone_acted_on_is_kept_even_when_empty(client, officer_token):
    from src.clustering import forget_orphans
    from src.database import get_connection

    rid = _report(client, type="fire", latitude=-1.26, longitude=36.86)
    cid = _cluster_of(rid)
    client.post(f"/clusters/{cid}/flags", json={"needs_location": True}, headers=_h(officer_token))
    conn = get_connection()
    conn.execute("DELETE FROM incidents WHERE id = ?", (rid,))
    assert forget_orphans(conn) == 0
    conn.commit()
    conn.close()
