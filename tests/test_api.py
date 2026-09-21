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


def _report_missing_child(client, **overrides):
    body = {
        "child_name": "Test Child",
        "age": 8,
        "physical_description": "short, curly hair",
        "last_seen_latitude": -1.28,
        "last_seen_longitude": 36.82,
        "last_seen_time": "2026-09-18 14:00",
        "reporter_phone": "+254700000000",
    }
    body.update(overrides)
    res = client.post("/missing-child/report", json=body)
    assert res.status_code == 200, res.text
    return res.json()["case_id"]


def test_missing_child_report_starts_hidden_from_public(client):
    case_id = _report_missing_child(client)
    public_ids = [c["id"] for c in client.get("/missing-child/cases").json()]
    assert case_id not in public_ids


def test_missing_child_public_response_never_includes_reporter_phone(client, officer_token):
    case_id = _report_missing_child(client)
    headers = {"Authorization": f"Bearer {officer_token}"}
    client.post(f"/missing-child/cases/{case_id}/verify", headers=headers)

    public_case = next(c for c in client.get("/missing-child/cases").json() if c["id"] == case_id)
    assert "reporter_phone" not in public_case

    officer_case = next(c for c in client.get("/missing-child/cases/all", headers=headers).json() if c["id"] == case_id)
    assert officer_case["reporter_phone"] == "+254700000000"


def test_missing_child_verification_flow(client, officer_token):
    case_id = _report_missing_child(client)
    headers = {"Authorization": f"Bearer {officer_token}"}

    # can't verify twice
    res = client.post(f"/missing-child/cases/{case_id}/verify", headers=headers)
    assert res.status_code == 200
    assert res.json()["status"] == "verified"
    assert res.json()["verified_by"] is not None

    res2 = client.post(f"/missing-child/cases/{case_id}/verify", headers=headers)
    assert res2.status_code == 400

    # now visible publicly
    public_ids = [c["id"] for c in client.get("/missing-child/cases").json()]
    assert case_id in public_ids


def test_missing_child_found_safe_is_public_found_deceased_is_not(client, officer_token):
    headers = {"Authorization": f"Bearer {officer_token}"}

    safe_id = _report_missing_child(client, child_name="Safe Child")
    client.post(f"/missing-child/cases/{safe_id}/verify", headers=headers)
    client.post(f"/missing-child/cases/{safe_id}/status", json={"status": "found_safe"}, headers=headers)

    deceased_id = _report_missing_child(client, child_name="Other Child")
    client.post(f"/missing-child/cases/{deceased_id}/verify", headers=headers)
    client.post(f"/missing-child/cases/{deceased_id}/status", json={"status": "found_deceased"}, headers=headers)

    public_ids = [c["id"] for c in client.get("/missing-child/cases").json()]
    assert safe_id in public_ids
    assert deceased_id not in public_ids


def test_missing_child_officer_endpoints_require_auth(client):
    case_id = _report_missing_child(client)
    assert client.get("/missing-child/cases/all").status_code == 401
    assert client.post(f"/missing-child/cases/{case_id}/verify").status_code == 401
    assert client.post(f"/missing-child/cases/{case_id}/status", json={"status": "found_safe"}).status_code == 401


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


# --- Missing Child broadcast + shared dispatch helpers -----------------------

def _add_subscribers(rows):
    from src.database import get_connection

    conn = get_connection()
    conn.executemany(
        "INSERT INTO subscribers (phone_number, area, latitude, longitude) VALUES (?, '', ?, ?)", rows
    )
    conn.commit()
    conn.close()


def _verified_case(client, headers, **overrides):
    case_id = _report_missing_child(client, **overrides)
    assert client.post(f"/missing-child/cases/{case_id}/verify", headers=headers).status_code == 200
    return case_id


def test_missing_child_broadcast_requires_a_verified_case(client, officer_token):
    headers = {"Authorization": f"Bearer {officer_token}"}
    case_id = _report_missing_child(client)  # still 'reported'
    res = client.post(f"/missing-child/cases/{case_id}/broadcast", json={}, headers=headers)
    assert res.status_code == 400
    assert client.get(f"/missing-child/cases/{case_id}/broadcast-preview", headers=headers).status_code == 400


def test_missing_child_broadcast_endpoints_require_officer_auth(client):
    case_id = _report_missing_child(client)
    assert client.post(f"/missing-child/cases/{case_id}/broadcast", json={}).status_code == 401
    assert client.get(f"/missing-child/cases/{case_id}/broadcast-preview").status_code == 401
    assert client.get("/missing-child/broadcasts").status_code == 401


def test_missing_child_broadcast_never_leaks_reporter_phone(client, officer_token):
    headers = {"Authorization": f"Bearer {officer_token}"}
    case_id = _verified_case(client, headers, reporter_phone="+254700123456")

    preview = client.get(f"/missing-child/cases/{case_id}/broadcast-preview", headers=headers).json()
    assert "700123456" not in preview["message"].replace(" ", "")
    assert "MISSING CHILD" in preview["message"] and len(preview["message"]) <= 300

    # an officer-typed message that includes the reporter's number, in any format, is refused
    for leaky in ("Call the parent on 0700 123 456", "contact +254700123456 now", "call 700-123-456"):
        res = client.post(f"/missing-child/cases/{case_id}/broadcast", json={"message": leaky}, headers=headers)
        assert res.status_code == 400, leaky


def test_missing_child_broadcast_targets_each_person_once_within_radius(client, officer_token):
    headers = {"Authorization": f"Bearer {officer_token}"}
    case_id = _verified_case(client, headers)  # last seen at (-1.28, 36.82)
    _add_subscribers([
        ("+254711111111", -1.281, 36.821),   # near
        ("0711 111 111", -1.282, 36.822),    # SAME person, different format, also near
        ("+254722222222", -1.285, 36.825),   # near, different person
        ("+254733333333", -1.50, 37.20),     # far away
    ])
    preview = client.get(f"/missing-child/cases/{case_id}/broadcast-preview?radius_km=10", headers=headers).json()
    assert preview["recipient_count"] == 2

    res = client.post(f"/missing-child/cases/{case_id}/broadcast", json={"radius_km": 10}, headers=headers)
    assert res.status_code == 200
    assert res.json()["recipients_reached"] == 2 and res.json()["failed_count"] == 0

    log = client.get("/missing-child/broadcasts", headers=headers).json()
    assert log[0]["case_id"] == case_id and log[0]["recipient_count"] == 2


class _AlwaysFailsProvider:
    def send(self, phone_number, message):
        return {"success": False, "error": "gateway down"}


def test_broadcasts_count_real_failures_not_attempts(client, officer_token, monkeypatch):
    import src.main as main_module

    monkeypatch.setattr(main_module, "get_provider", lambda: _AlwaysFailsProvider())
    headers = {"Authorization": f"Bearer {officer_token}"}
    _add_subscribers([("+254711111111", -1.281, 36.821), ("+254722222222", -1.285, 36.825)])

    case_id = _verified_case(client, headers)
    res = client.post(f"/missing-child/cases/{case_id}/broadcast", json={"radius_km": 10}, headers=headers).json()
    assert res["recipients_reached"] == 0 and res["failed_count"] == 2

    inc_id = _report(client, type="robbery", latitude=-1.28, longitude=36.82)
    res2 = client.post(
        "/alert/broadcast", json={"incident_id": inc_id, "message": "Stay alert.", "radius_km": 10}, headers=headers
    ).json()
    assert res2["recipients_reached"] == 0 and res2["failed_count"] == 2


def test_firms_detections_reach_officers_recommended_list(client, officer_token):
    """Regression: /alert/recommended only shows scoring_stage='strategic'
    rows, and scripts/ingest_firms.py used to insert with the DB default
    ('reflex'), so real satellite fires silently could never reach an
    officer. Found by looking at the dashboard in a real browser."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from ingest_firms import insert_fire_detection

    from src.database import get_connection

    conn = get_connection()
    insert_fire_detection(conn, -0.5, 37.0, "2026-09-21 12:00", "NASA FIRMS test detection", "Low", frp=150.0)
    conn.commit()
    conn.close()

    headers = {"Authorization": f"Bearer {officer_token}"}
    recommended = client.get("/alert/recommended", headers=headers).json()
    fires = [r for r in recommended if r["source"] == "bulk" and r["type"] == "fire"]
    assert len(fires) == 1
    assert fires[0]["scoring_stage"] == "strategic"
    assert fires[0]["alert_tier"] in ("sms_recommended", "critical")


def test_security_headers_present_on_api_and_static_responses(client):
    for path in ("/health", "/dashboard/"):
        res = client.get(path)
        assert res.headers["x-content-type-options"] == "nosniff", path
        assert res.headers["x-frame-options"] == "DENY", path
        assert res.headers["referrer-policy"] == "no-referrer", path


def test_dashboard_is_revalidated_not_heuristically_cached(client):
    assert client.get("/dashboard/").headers["cache-control"] == "no-cache"


# --- Input validation ----------------------------------------------------------

def _incident_body(**overrides):
    body = {"type": "flood", "latitude": -1.19, "longitude": 36.90, "timestamp": "2026-09-12 10:00"}
    body.update(overrides)
    return body


def test_incident_rejects_coordinates_outside_service_area(client):
    for lat, lon in ((0.0, 0.0), (48.85, 2.35), (91, 36.8), (-1.28, 181), (-1.28, 60.0)):
        res = client.post("/report/citizen", json=_incident_body(latitude=lat, longitude=lon))
        assert res.status_code == 422, (lat, lon)


def test_incident_rejects_malformed_and_far_future_timestamps(client):
    for bad in ("yesterday", "2026-09-12", "12/09/2026 10:00", "2026-09-12T10:00", "", "2026-13-45 99:99"):
        assert client.post("/report/citizen", json=_incident_body(timestamp=bad)).status_code == 422, bad
    assert client.post("/report/citizen", json=_incident_body(timestamp="2099-01-01 10:00")).status_code == 422
    # past dates stay valid (officer back-fills)
    assert client.post("/report/citizen", json=_incident_body(timestamp="2020-01-01 10:00")).status_code == 200


def test_incident_type_must_be_known_and_is_normalised(client):
    assert client.post("/report/citizen", json=_incident_body(type="totally-made-up")).status_code == 422
    assert client.post("/report/citizen", json=_incident_body(type="<img onerror=x>")).status_code == 422
    assert client.post("/report/citizen", json=_incident_body(type="  FLOOD ")).status_code == 200
    stored = client.get("/incidents").json()[0]
    assert stored["type"] == "flood"


def test_free_text_and_bulk_size_limits(client, officer_token):
    assert client.post("/report/citizen", json=_incident_body(description="x" * 1001)).status_code == 422
    assert client.post("/report/citizen", json=_incident_body(area="x" * 201)).status_code == 422
    too_many = {"officer_id": "o", "reports": [_incident_body()] * 501}
    res = client.post("/report/bulk", json=too_many, headers={"Authorization": f"Bearer {officer_token}"})
    assert res.status_code == 422


def test_phone_numbers_are_normalised_or_rejected(client):
    good = {
        "+254712345678": "+254712345678",
        "0712 345 678": "+254712345678",
        "254712345678": "+254712345678",
        "+254-712-345-678": "+254712345678",
        "712345678": "+254712345678",
        "0112345678": "+254112345678",
        "+447911123456": "+447911123456",  # other countries: E.164 only
    }
    for raw, expected in good.items():
        res = client.post("/subscribers", json={"phone_number": raw, "latitude": -1.28, "longitude": 36.82})
        assert res.status_code == 200, raw
        from src.database import get_connection

        conn = get_connection()
        stored = [r["phone_number"] for r in conn.execute("SELECT phone_number FROM subscribers")]
        conn.close()
        assert expected in stored, (raw, stored)

    for bad in ("", "abc", "12345", "+2547123", "0612345678", "+254 812 345 678", "07123456789012"):
        res = client.post("/subscribers", json={"phone_number": bad, "latitude": -1.28, "longitude": 36.82})
        assert res.status_code == 422, bad


def test_subscribing_twice_updates_instead_of_duplicating(client):
    from src.database import get_connection

    first = client.post("/subscribers", json={"phone_number": "0712 345 678", "latitude": -1.28, "longitude": 36.82})
    assert first.json()["status"] == "subscribed"
    second = client.post("/subscribers", json={"phone_number": "+254712345678", "area": "moved", "latitude": -1.5, "longitude": 36.9})
    assert second.json()["status"] == "updated"

    conn = get_connection()
    rows = conn.execute("SELECT phone_number, area, latitude FROM subscribers").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["area"] == "moved" and rows[0]["latitude"] == -1.5


def test_missing_child_report_validation(client):
    base = {
        "child_name": "Test Child", "age": 8, "physical_description": "short",
        "last_seen_latitude": -1.28, "last_seen_longitude": 36.82,
        "last_seen_time": "2026-09-18 14:00", "reporter_phone": "0700 000 000",
    }
    ok = client.post("/missing-child/report", json=base)
    assert ok.status_code == 200
    for patch in ({"reporter_phone": "nope"}, {"last_seen_time": "later"}, {"last_seen_latitude": 0.0, "last_seen_longitude": 0.0},
                  {"age": 25}, {"child_name": "x" * 101}):
        assert client.post("/missing-child/report", json={**base, **patch}).status_code == 422, patch
    # the stored reporter phone is the normalised form
    from src.database import get_connection

    conn = get_connection()
    assert conn.execute("SELECT reporter_phone FROM missing_child_cases").fetchone()["reporter_phone"] == "+254700000000"
    conn.close()


# --- Risk heatmap --------------------------------------------------------------

def _heatmap(client, **params):
    q = {"lat_min": -1.5, "lon_min": 36.6, "lat_max": -1.0, "lon_max": 37.1, "size": 12}
    q.update(params)
    return client.get("/risk/heatmap", params=q)


def test_heatmap_cell_values_equal_point_risk_at_the_cell_centre(client):
    """The whole point of not reusing build_risk_grid: a cell's colour must
    mean the same as /predict there, at any zoom."""
    from src.risk_surface import point_risk

    res = _heatmap(client, category="crime")
    assert res.status_code == 200
    body = res.json()
    assert body["cells"], "the synthetic baseline should put some risk around Nairobi"
    for cell in body["cells"]:
        expected, _ = point_risk(cell["lat"], cell["lon"], "robbery")  # robbery -> crime
        assert abs(cell["risk"] - expected) < 1e-3, cell
        assert 0.05 <= cell["risk"] <= 1.0
    assert body["category"] == "crime"
    assert abs(body["cell_lat_deg"] - 0.5 / 12) < 1e-9


def test_heatmap_is_comparable_across_viewports(client):
    """A tiny box around a quiet area must NOT be renormalised to look red."""
    wide = {(round(c["lat"], 2), round(c["lon"], 2)): c["risk"] for c in _heatmap(client, size=30).json()["cells"]}
    quiet = _heatmap(client, lat_min=-4.4, lon_min=39.0, lat_max=-4.3, lon_max=39.1, size=10).json()["cells"]
    assert all(c["risk"] < 0.5 for c in quiet)
    assert max(wide.values()) > 0.3  # sanity: the busy Nairobi box does have real hotspots


def test_heatmap_clamps_to_service_area_and_validates_input(client):
    assert _heatmap(client, lat_min=45, lat_max=48, lon_min=2, lon_max=5).json()["cells"] == []  # Europe
    assert _heatmap(client, lat_min=-30, lat_max=30, lon_min=10, lon_max=60).status_code == 200  # clamped, not rejected
    assert _heatmap(client, lat_min=-1.0, lat_max=-1.5).status_code == 400
    assert _heatmap(client, size=100).status_code == 422
    assert _heatmap(client, size=2).status_code == 422
    assert _heatmap(client, category="everything").status_code == 422


def test_incident_list_limit_is_bounded(client, officer_token):
    """?limit=1000000 used to dump the whole table on a public endpoint."""
    headers = {"Authorization": f"Bearer {officer_token}"}
    assert client.get("/incidents?limit=1000000").status_code == 422
    assert client.get("/incidents?limit=0").status_code == 422
    assert client.get("/incidents?limit=500").status_code == 200
    assert client.get("/incidents/all?limit=501", headers=headers).status_code == 422


# --- Satellite fire urgency from radiative power ---------------------------------

def _fires_by_source(client, officer_token):
    headers = {"Authorization": f"Bearer {officer_token}"}
    return client.get("/incidents/all?limit=500", headers=headers).json()


def _insert_fire(frp, lat=-0.5, lon=37.0):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from ingest_firms import insert_fire_detection

    from src.database import get_connection

    conn = get_connection()
    insert_fire_detection(conn, lat, lon, "2026-09-21 12:00", f"FIRMS test FRP={frp}", "Low", frp=frp)
    conn.commit()
    conn.close()


def test_fire_detection_tier_bands_and_corroboration_bump():
    from src.confidence import fire_detection_tier

    assert fire_detection_tier(10.0, False) == "in_app"
    assert fire_detection_tier(24.9, False) == "in_app"
    assert fire_detection_tier(25.0, False) == "sms_recommended"
    assert fire_detection_tier(99.9, False) == "sms_recommended"
    assert fire_detection_tier(100.0, False) == "critical"
    # a human report nearby bumps one level, capped at critical
    assert fire_detection_tier(10.0, True) == "sms_recommended"
    assert fire_detection_tier(50.0, True) == "critical"
    assert fire_detection_tier(500.0, True) == "critical"


def test_satellite_fires_are_ranked_by_power_not_all_critical(client, officer_token):
    _insert_fire(12.0, lat=-0.5)     # weak
    _insert_fire(60.0, lat=-1.5)     # medium
    _insert_fire(220.0, lat=-2.5)    # large
    fires = {f["magnitude"]: f for f in _fires_by_source(client, officer_token) if f["source"] == "bulk"}
    assert fires[12.0]["alert_tier"] == "in_app"
    assert fires[60.0]["alert_tier"] == "sms_recommended"
    assert fires[220.0]["alert_tier"] == "critical"
    # confidence (is it real) is unchanged and high for all of them
    assert all(f["confidence_score"] == 0.8 for f in fires.values())

    headers = {"Authorization": f"Bearer {officer_token}"}
    recommended = {f["magnitude"] for f in client.get("/alert/recommended", headers=headers).json()}
    assert recommended == {60.0, 220.0}  # the 12 MW detection is visible on the map but not recommended


def test_confirm_vote_keeps_satellite_fire_on_its_power_based_tier(client, officer_token):
    _insert_fire(12.0)
    fire = next(f for f in _fires_by_source(client, officer_token) if f["source"] == "bulk")
    assert fire["alert_tier"] == "in_app"

    voted = client.post(f"/report/{fire['id']}/vote", json={"confirm": True}).json()
    # a confirm is corroboration: one level up (sms_recommended), NOT the critical that
    # deriving the tier from confidence (0.8 + 0.15) would have produced
    assert voted["alert_tier"] == "sms_recommended"
    assert voted["magnitude"] == 12.0


def test_citizen_report_nearby_bumps_a_satellite_fire(client, officer_token):
    _report(client, type="fire", latitude=-0.5, longitude=37.0, timestamp="2026-09-21 12:30")
    _insert_fire(12.0, lat=-0.5, lon=37.0)
    fire = next(f for f in _fires_by_source(client, officer_token) if f["source"] == "bulk")
    assert fire["alert_tier"] == "sms_recommended"  # 12 MW would be in_app without the human report
