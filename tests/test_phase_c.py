"""
Phase C: the officer console (static/officer.*) and the endpoints it added.
"""
import re
from pathlib import Path

from test_api import _report

STATIC = Path(__file__).resolve().parent.parent / "static"


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def _force_recommended(inc_id):
    from src.database import get_connection

    conn = get_connection()
    conn.execute("UPDATE incidents SET alert_tier = 'critical', scoring_stage = 'strategic' WHERE id = ?", (inc_id,))
    conn.commit()
    conn.close()


def test_dismissed_reports_leave_the_recommended_list_verified_go_first(client, officer_token, owner_token):
    a = _report(client, type="flood", latitude=-1.19, longitude=36.90)
    b = _report(client, type="flood", latitude=-1.30, longitude=36.80)
    _force_recommended(a)
    _force_recommended(b)

    dismissed = client.post(f"/incidents/{a}/review", json={"action": "dismiss", "note": "duplicate"},
                            headers=_h(officer_token))
    assert dismissed.status_code == 200 and dismissed.json()["review_status"] == "dismissed"
    client.post(f"/incidents/{b}/review", json={"action": "verify"}, headers=_h(officer_token))

    ids = [r["id"] for r in client.get("/alert/recommended", headers=_h(officer_token)).json()]
    assert a not in ids and ids[0] == b

    log = client.get("/audit-log", headers=_h(owner_token)).json()
    assert {e["action"] for e in log} >= {"dismiss_incident", "verify_incident"}


def test_review_needs_sign_in_and_a_real_incident(client, officer_token):
    assert client.post("/incidents/1/review", json={"action": "verify"}).status_code == 401
    assert client.post("/incidents/99999/review", json={"action": "verify"},
                       headers=_h(officer_token)).status_code == 404
    assert client.post("/incidents/1/review", json={"action": "delete"},
                       headers=_h(officer_token)).status_code == 422


def test_officer_console_is_served_with_a_strict_csp(client):
    res = client.get("/dashboard/officer.html")
    assert res.status_code == 200
    csp = res.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    # the full-map page still has inline script, so it must not get this header (it would break)
    assert "content-security-policy" not in client.get("/dashboard/map.html").headers


def test_officer_console_has_no_inline_code_and_never_uses_innerhtml():
    html = (STATIC / "officer.html").read_text(encoding="utf-8")
    js = (STATIC / "officer.js").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline <script> would be blocked by the CSP"
    assert "<style" not in html and not re.search(r"\sstyle=", html)
    assert not re.search(r"\son[a-z]+=", html), "inline event handlers would be blocked by the CSP"
    # text from the public reaches the page only through textContent
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "document.write" not in js


def test_no_page_loads_scripts_or_styles_from_a_cdn():
    for page in ("index.html", "map.html", "officer.html"):
        html = (STATIC / page).read_text(encoding="utf-8")
        assert "unpkg.com" not in html and "cdn" not in html.lower(), page
