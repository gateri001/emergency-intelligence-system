def _report(client, **overrides):
    body = {
        "type": "flood",
        "latitude": -1.19,
        "longitude": 36.90,
        "description": "test report",
        "timestamp": "2026-09-12 10:00",
    }
    body.update(overrides)
    res = client.post("/report/citizen", json=body)
    assert res.status_code == 200, res.text
    return res.json()["incident_id"]


def test_officers_only_report_excluded_from_public_feed(client):
    _report(client, visibility="officers_only", description="witnessed crime")
    _report(client, visibility="public", description="normal flood report")

    feed = client.get("/incidents").json()
    assert len(feed) == 1
    assert feed[0]["visibility"] == "public"


def test_officer_feed_includes_officers_only_reports(client, officer_token):
    _report(client, visibility="officers_only", description="witnessed crime")

    res = client.get("/incidents/all", headers={"Authorization": f"Bearer {officer_token}"})
    assert res.status_code == 200
    assert len(res.json()) == 1
    assert res.json()[0]["visibility"] == "officers_only"


def test_officer_only_endpoints_reject_unauthenticated_requests(client):
    assert client.get("/incidents/all").status_code == 401
    assert client.get("/alert/recommended").status_code == 401


def test_flood_report_with_evidence_starts_above_in_app_only(client):
    inc_id = _report(client, type="flood", has_evidence=True)
    feed = client.get("/incidents").json()
    report = next(r for r in feed if r["id"] == inc_id)
    # base 0.35 (hazard) + 0.15 (evidence) = 0.50 -> sms_recommended (hazard threshold 0.30)
    assert report["confidence_score"] == 0.50
    assert report["alert_tier"] == "sms_recommended"


def test_crime_report_needs_more_to_reach_sms_tier_than_flood(client):
    crime_id = _report(client, type="robbery", has_evidence=True, latitude=-1.28, longitude=36.82)
    flood_id = _report(client, type="flood", has_evidence=True, latitude=-1.19, longitude=36.90)

    feed = {r["id"]: r for r in client.get("/incidents").json()}
    # crime base 0.15 + evidence 0.15 = 0.30, below crime's 0.50 sms threshold
    assert feed[crime_id]["alert_tier"] == "in_app"
    # flood base 0.35 + evidence 0.15 = 0.50, at/above hazard's 0.30 sms threshold
    assert feed[flood_id]["alert_tier"] == "sms_recommended"


def test_vote_confirm_raises_confidence_and_dispute_lowers_it(client):
    inc_id = _report(client, type="flood")
    baseline = client.get("/incidents").json()[0]["confidence_score"]

    up = client.post(f"/report/{inc_id}/vote", json={"confirm": True}).json()
    assert up["confidence_score"] > baseline

    down = client.post(f"/report/{inc_id}/vote", json={"confirm": False}).json()
    assert down["confidence_score"] < up["confidence_score"]


def test_officer_report_starts_at_high_confidence_without_corroboration(client, officer_token):
    res = client.post(
        "/report/officer",
        json={"type": "robbery", "latitude": -1.28, "longitude": 36.82, "timestamp": "2026-09-12 10:00"},
        headers={"Authorization": f"Bearer {officer_token}"},
    )
    assert res.status_code == 200
    all_incidents = client.get("/incidents/all", headers={"Authorization": f"Bearer {officer_token}"}).json()
    assert all_incidents[0]["confidence_score"] == 0.9
    assert all_incidents[0]["alert_tier"] == "critical"


def test_citizen_report_endpoint_is_rate_limited(client):
    statuses = [
        client.post(
            "/report/citizen",
            json={"type": "fire", "latitude": -1.0, "longitude": 36.8, "timestamp": "2026-09-12 10:00"},
        ).status_code
        for _ in range(12)
    ]
    assert statuses.count(200) == 10
    assert statuses.count(429) == 2


def test_login_rejects_bad_credentials(client, officer_token):
    res = client.post("/token", data={"username": "test_officer", "password": "wrong"})
    assert res.status_code == 400
