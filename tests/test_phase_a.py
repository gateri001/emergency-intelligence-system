"""
Phase A: roles, crime-reporter protection, and the tightened Missing Child
Alert (OB number, expiry, two-person rule, first name only, follow-up).
"""
from cryptography.fernet import Fernet

from src.database import get_connection
from test_api import OB, _add_subscribers, _report, _report_missing_child


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def _age_incident(inc_id, minutes=20):
    """Moves an incident's arrival time into the past (past the crime delay)."""
    conn = get_connection()
    conn.execute(f"UPDATE incidents SET created_at = datetime('now', '-{minutes} minutes') WHERE id = ?", (inc_id,))
    conn.commit()
    conn.close()


# --- crime privacy -----------------------------------------------------------

def test_crime_defaults_to_officers_only_other_types_to_public(client, officer_token):
    crime_id = _report(client, type="robbery")
    flood_id = _report(client, type="flood")
    rows = {r["id"]: r for r in client.get("/incidents/all", headers=_h(officer_token)).json()}
    assert rows[crime_id]["visibility"] == "officers_only"
    assert rows[flood_id]["visibility"] == "public"


def test_public_crime_is_delayed_then_shown_blurred(client, officer_token):
    exact = (-1.28137, 36.82291)
    inc_id = _report(client, type="robbery", visibility="public", latitude=exact[0], longitude=exact[1])

    assert inc_id not in [r["id"] for r in client.get("/incidents").json()]
    assert client.post(f"/report/{inc_id}/vote", json={"confirm": True}).status_code == 404

    _age_incident(inc_id)
    public = {r["id"]: r for r in client.get("/incidents").json()}[inc_id]
    assert (public["latitude"], public["longitude"]) != exact
    assert abs(public["latitude"] - exact[0]) < 0.0045 and abs(public["longitude"] - exact[1]) < 0.0045
    # the same blurred answer every time, so it can't be averaged away
    again = {r["id"]: r for r in client.get("/incidents").json()}[inc_id]
    assert (again["latitude"], again["longitude"]) == (public["latitude"], public["longitude"])
    voted = client.post(f"/report/{inc_id}/vote", json={"confirm": True}).json()
    assert (voted["latitude"], voted["longitude"]) == (public["latitude"], public["longitude"])

    officer_view = {r["id"]: r for r in client.get("/incidents/all", headers=_h(officer_token)).json()}[inc_id]
    assert (officer_view["latitude"], officer_view["longitude"]) == exact


def test_phone_is_refused_on_non_crime_reports(client):
    res = client.post("/report/citizen", json={
        "type": "flood", "latitude": -1.19, "longitude": 36.90, "timestamp": "2026-09-12 10:00",
        "reporter_phone": "0712345678"})
    assert res.status_code == 422


def test_crime_phone_without_key_is_not_stored_and_reporter_is_told(client, monkeypatch):
    monkeypatch.delenv("EIS_DATA_KEY", raising=False)
    res = client.post("/report/citizen", json={
        "type": "robbery", "latitude": -1.28, "longitude": 36.82, "timestamp": "2026-09-12 10:00",
        "reporter_phone": "0712345678"}).json()
    assert res["phone_stored"] is False
    conn = get_connection()
    stored = conn.execute("SELECT reporter_phone_enc FROM incidents WHERE id = ?", (res["incident_id"],)).fetchone()[0]
    conn.close()
    assert stored is None


def test_crime_phone_is_encrypted_hidden_and_revealed_only_with_reason(
        client, monkeypatch, officer_token, admin_token, owner_token):
    monkeypatch.setenv("EIS_DATA_KEY", Fernet.generate_key().decode())
    res = client.post("/report/citizen", json={
        "type": "assault", "latitude": -1.28, "longitude": 36.82, "timestamp": "2026-09-12 10:00",
        "reporter_phone": "0712 345 678"}).json()
    inc_id = res["incident_id"]
    assert res["phone_stored"] is True

    conn = get_connection()
    stored = conn.execute("SELECT reporter_phone_enc FROM incidents WHERE id = ?", (inc_id,)).fetchone()[0]
    conn.close()
    assert stored and "712345678" not in stored

    # no officer listing carries it, in any field
    for path in ("/incidents/all", "/alert/recommended"):
        assert "712345678" not in client.get(path, headers=_h(officer_token)).text

    reveal = f"/incidents/{inc_id}/reveal-reporter"
    reason = {"reason": "Witness statement needed by investigating officer"}
    assert client.post(reveal, json=reason, headers=_h(officer_token)).status_code == 403
    assert client.post(reveal, json={"reason": "because"}, headers=_h(admin_token)).status_code == 422
    opened = client.post(reveal, json=reason, headers=_h(admin_token))
    assert opened.status_code == 200 and opened.json()["reporter_phone"] == "+254712345678"

    assert client.get("/audit-log", headers=_h(admin_token)).status_code == 403
    log = client.get("/audit-log", headers=_h(owner_token)).json()
    entry = next(e for e in log if e["action"] == "reveal_reporter_phone")
    assert entry["actor"] == "test_admin" and entry["target_id"] == inc_id
    assert entry["detail"] == reason["reason"]


def test_deleted_account_token_stops_working_at_once(client, officer_token):
    assert client.get("/incidents/all", headers=_h(officer_token)).status_code == 200
    conn = get_connection()
    conn.execute("DELETE FROM officers WHERE username = 'test_officer'")
    conn.commit()
    conn.close()
    assert client.get("/incidents/all", headers=_h(officer_token)).status_code == 401


def test_me_reports_role(client, admin_token):
    assert client.get("/me", headers=_h(admin_token)).json() == {"username": "test_admin", "role": "admin"}


# --- nearby + incident broadcast preview --------------------------------------

def test_nearby_returns_risk_per_category_and_close_public_incidents(client):
    near = _report(client, type="flood", latitude=-1.19, longitude=36.90)
    _report(client, type="flood", latitude=-1.40, longitude=36.60)  # ~40 km away
    hidden = _report(client, type="robbery", latitude=-1.191, longitude=36.901)  # officers-only by default

    body = client.get("/nearby?latitude=-1.19&longitude=36.90").json()
    assert set(body["risk"]) == {"crime", "hazard", "medical"}
    ids = [i["id"] for i in body["incidents"]]
    assert ids == [near] and hidden not in ids

    assert client.get("/nearby?latitude=10&longitude=10").status_code == 400


def test_incident_broadcast_preview_counts_without_sending(client, officer_token):
    inc_id = _report(client, type="flood", latitude=-1.28, longitude=36.82)
    _add_subscribers([("+254711111111", -1.281, 36.821), ("+254733333333", -1.50, 37.20)])
    body = {"incident_id": inc_id, "message": "Flooding near you. Move to higher ground.", "radius_km": 5}
    preview = client.post("/alert/broadcast-preview", json=body, headers=_h(officer_token)).json()
    assert preview["recipient_count"] == 1
    assert client.get("/alert/broadcasts", headers=_h(officer_token)).json() == []


# --- Missing Child Alert -------------------------------------------------------

def _verified(client, token, **overrides):
    case_id = _report_missing_child(client, **overrides)
    assert client.post(f"/missing-child/cases/{case_id}/verify", json=OB, headers=_h(token)).status_code == 200
    return case_id


def _expire(case_id):
    conn = get_connection()
    conn.execute("UPDATE missing_child_cases SET expires_at = datetime('now', '-1 minute') WHERE id = ?", (case_id,))
    conn.commit()
    conn.close()


def test_verify_requires_the_police_ob_number(client, officer_token):
    case_id = _report_missing_child(client)
    url = f"/missing-child/cases/{case_id}/verify"
    assert client.post(url, headers=_h(officer_token)).status_code == 422
    assert client.post(url, json={"ob_number": "  "}, headers=_h(officer_token)).status_code == 422
    res = client.post(url, json=OB, headers=_h(officer_token)).json()
    assert res["ob_number"] == OB["ob_number"] and res["expires_at"]


def test_expired_case_leaves_public_view_until_renewed(client, officer_token, owner_token):
    case_id = _verified(client, officer_token)
    _expire(case_id)
    assert case_id not in [c["id"] for c in client.get("/missing-child/cases").json()]
    assert client.post(f"/missing-child/cases/{case_id}/broadcast", json={}, headers=_h(owner_token)).status_code == 400

    renew = f"/missing-child/cases/{case_id}/renew"
    assert client.post(renew, json={"note": ""}, headers=_h(officer_token)).status_code == 422
    assert client.post(renew, json={"note": "Called Kayole station: still missing"},
                       headers=_h(officer_token)).status_code == 200
    assert case_id in [c["id"] for c in client.get("/missing-child/cases").json()]


def test_sms_uses_first_name_only(client, officer_token):
    case_id = _verified(client, officer_token, child_name="Amina Wanjiru Otieno")
    msg = client.get(f"/missing-child/cases/{case_id}/broadcast-preview", headers=_h(officer_token)).json()["message"]
    assert "Amina" in msg and "Wanjiru" not in msg and "Otieno" not in msg


def test_two_person_rule(client, officer_token, admin_token, second_admin_token, owner_token):
    _add_subscribers([("+254711111111", -1.281, 36.821), ("+254722222222", -1.285, 36.825)])
    case_id = _verified(client, officer_token)

    # only the owner may send alone
    direct = f"/missing-child/cases/{case_id}/broadcast"
    assert client.post(direct, json={}, headers=_h(officer_token)).status_code == 403
    assert client.post(direct, json={}, headers=_h(admin_token)).status_code == 403

    prop = client.post(f"/missing-child/cases/{case_id}/broadcast-proposals", json={"radius_km": 10},
                       headers=_h(officer_token)).json()
    assert prop["status"] == "pending" and prop["recipient_count_now"] == 2
    assert client.get("/missing-child/broadcasts", headers=_h(officer_token)).json() == []  # nothing sent yet

    approve = f"/missing-child/broadcast-proposals/{prop['id']}/approve"
    assert client.post(approve, headers=_h(officer_token)).status_code == 403  # officers can't approve
    sent = client.post(approve, headers=_h(admin_token))
    assert sent.status_code == 200 and sent.json()["recipients_reached"] == 2
    assert sent.json()["message"] == prop["message"]
    assert client.post(approve, headers=_h(second_admin_token)).status_code == 400  # never sent twice

    log = client.get("/missing-child/broadcasts", headers=_h(officer_token)).json()
    assert log[0]["triggered_by"] == "test_officer" and log[0]["approved_by"] == "test_admin"

    # an admin can't approve their own proposal; a second admin can reject it
    own = client.post(f"/missing-child/cases/{case_id}/broadcast-proposals", json={},
                      headers=_h(admin_token)).json()
    assert client.post(f"/missing-child/broadcast-proposals/{own['id']}/approve",
                       headers=_h(admin_token)).status_code == 403
    rejected = client.post(f"/missing-child/broadcast-proposals/{own['id']}/reject", headers=_h(second_admin_token))
    assert rejected.json()["status"] == "rejected"

    assert client.post(direct, json={"radius_km": 10}, headers=_h(owner_token)).status_code == 200


def test_resolving_a_case_cancels_pending_proposals(client, officer_token, admin_token):
    case_id = _verified(client, officer_token)
    prop = client.post(f"/missing-child/cases/{case_id}/broadcast-proposals", json={},
                       headers=_h(officer_token)).json()
    client.post(f"/missing-child/cases/{case_id}/status", json={"status": "found_safe"}, headers=_h(officer_token))
    res = client.post(f"/missing-child/broadcast-proposals/{prop['id']}/approve", headers=_h(admin_token))
    assert res.status_code == 400


class _Recorder:
    def __init__(self):
        self.sent_to = []

    def send(self, phone_number, message):
        self.sent_to.append(phone_number)
        return {"success": True}


def test_followup_reaches_exactly_the_people_alerted_once(client, officer_token, owner_token, monkeypatch):
    import src.main as main_module

    case_id = _verified(client, officer_token, child_name="Brian Kamau")
    _add_subscribers([("+254711111111", -1.281, 36.821)])
    client.post(f"/missing-child/cases/{case_id}/broadcast", json={"radius_km": 10}, headers=_h(owner_token))
    _add_subscribers([("+254722222222", -1.282, 36.822)])  # signed up after the alert

    followup = f"/missing-child/cases/{case_id}/followup"
    assert client.post(followup, headers=_h(officer_token)).status_code == 400  # not resolved yet
    client.post(f"/missing-child/cases/{case_id}/status", json={"status": "found_safe"}, headers=_h(officer_token))

    recorder = _Recorder()
    monkeypatch.setattr(main_module, "get_provider", lambda: recorder)
    res = client.post(followup, headers=_h(officer_token)).json()
    assert recorder.sent_to == ["+254711111111"]
    assert "Brian" in res["message"] and "Kamau" not in res["message"]
    assert client.post(followup, headers=_h(officer_token)).status_code == 400
