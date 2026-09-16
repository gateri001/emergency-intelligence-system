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


def test_vote_endpoint_cannot_leak_officers_only_report(client):
    inc_id = _report(client, visibility="officers_only", type="robbery",
                      description="witness at risk, must not leak via vote")

    res = client.post(f"/report/{inc_id}/vote", json={"confirm": True})
    assert res.status_code == 404
    assert "witness at risk" not in res.text
    assert res.json() == {"detail": "Incident not found"}


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


def test_broadcast_warns_when_source_was_officers_only(client, officer_token):
    inc_id = _report(client, visibility="officers_only", type="robbery")
    headers = {"Authorization": f"Bearer {officer_token}"}

    res = client.post(
        "/alert/broadcast",
        json={"incident_id": inc_id, "message": "Stay alert in the area.", "radius_km": 5},
        headers=headers,
    )
    assert res.status_code == 200
    assert "officers_only" in res.json()["reporter_safety_warning"]

    public_id = _report(client, visibility="public", type="robbery", latitude=-1.3, longitude=36.9)
    res2 = client.post(
        "/alert/broadcast",
        json={"incident_id": public_id, "message": "Stay alert in the area.", "radius_km": 5},
        headers=headers,
    )
    assert res2.status_code == 200
    assert res2.json()["reporter_safety_warning"] is None


def test_report_ends_at_strategic_scoring_stage_after_background_refinement(client):
    inc_id = _report(client, type="flood")
    row = next(r for r in client.get("/incidents").json() if r["id"] == inc_id)
    assert row["scoring_stage"] == "strategic"


def test_recommended_alerts_excludes_reflex_only_rows_even_at_high_tier(client, officer_token):
    from src.database import get_connection

    # An officer report normally reaches scoring_stage='strategic' with a
    # critical tier via the background task - simulate the row being stuck
    # at reflex-only (e.g. strategic refinement hasn't run yet, or failed)
    # to prove it can never masquerade as a real recommendation.
    inc_id = _report(client, type="robbery")
    conn = get_connection()
    conn.execute(
        "UPDATE incidents SET alert_tier = 'critical', confidence_score = 0.95, scoring_stage = 'reflex' WHERE id = ?",
        (inc_id,),
    )
    conn.commit()
    conn.close()

    headers = {"Authorization": f"Bearer {officer_token}"}
    recommended_ids = [r["id"] for r in client.get("/alert/recommended", headers=headers).json()]
    assert inc_id not in recommended_ids

    conn = get_connection()
    conn.execute("UPDATE incidents SET scoring_stage = 'strategic' WHERE id = ?", (inc_id,))
    conn.commit()
    conn.close()
    recommended_ids = [r["id"] for r in client.get("/alert/recommended", headers=headers).json()]
    assert inc_id in recommended_ids


def test_strategic_nearby_corroboration_excludes_the_reports_own_row(client):
    """Regression test for the reflex/strategic split: strategic refinement
    now runs AFTER insert, not before, so count_nearby_reports() must
    exclude a report's own row or every report would trivially "corroborate
    itself" (same location, same timestamp) and inflate its own count."""
    first_id = _report(client, type="flood", latitude=-1.19, longitude=36.90, description="first")
    first = next(r for r in client.get("/incidents").json() if r["id"] == first_id)
    # base 0.35, no nearby reports yet - must not count itself
    assert first["confidence_score"] == 0.35

    second_id = _report(client, type="flood", latitude=-1.1901, longitude=36.9001, description="second, nearby")
    feed = {r["id"]: r for r in client.get("/incidents").json()}
    # each should now see exactly ONE nearby report (the other one), not two
    # (which would happen if a report's own row weren't excluded)
    assert feed[second_id]["confidence_score"] == 0.55  # 0.35 base + 0.20 (1 nearby report)


def test_flood_report_inside_known_flood_zone_gets_ground_truth_bonus(client):
    import json

    from src.database import get_connection

    # A simple square "known flood zone" around (-1.19, 36.90)
    square = {
        "type": "Polygon",
        "coordinates": [[[36.89, -1.20], [36.91, -1.20], [36.91, -1.18], [36.89, -1.18], [36.89, -1.20]]],
    }
    conn = get_connection()
    conn.execute(
        "INSERT INTO flood_extents (event_code, region, geojson, source_date) VALUES (?, ?, ?, ?)",
        ("TEST01", "test_region", json.dumps(square), "2024-01-01"),
    )
    conn.commit()
    conn.close()

    inside_id = _report(client, type="flood", latitude=-1.19, longitude=36.90)  # inside the square
    outside_id = _report(client, type="flood", latitude=-2.50, longitude=38.50)  # nowhere near it

    feed = {r["id"]: r for r in client.get("/incidents").json()}
    # base 0.35 (hazard) + ground truth 0.20 = 0.55
    assert feed[inside_id]["confidence_score"] == 0.55
    # base 0.35, no ground truth bonus
    assert feed[outside_id]["confidence_score"] == 0.35


def test_nearest_facilities_sorted_by_distance(client):
    from src.database import get_connection

    conn = get_connection()
    conn.execute(
        "INSERT INTO health_facilities (osm_id, name, amenity, has_emergency, latitude, longitude) VALUES "
        "('1', 'Far Clinic', 'clinic', NULL, -1.30, 36.90), "
        "('2', 'Near Hospital', 'hospital', 'yes', -1.2865, 36.8175)"
    )
    conn.commit()
    conn.close()

    res = client.get("/facilities/nearest?latitude=-1.286389&longitude=36.817223&limit=5")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 2
    assert data[0]["name"] == "Near Hospital"
    assert data[0]["distance_km"] < data[1]["distance_km"]
