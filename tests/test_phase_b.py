"""
Phase B: the mobile citizen app (static/index.html, app.js, app.css) and the
two deployment safety switches it relies on.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    m = re.search(rf"(?:async )?function {name}\([^)]*\) \{{.*?\n\}}", APP_JS, re.S)
    assert m, f"could not find function {name} in static/app.js"
    return m.group(0)


def test_config_is_demo_on_signup_off_by_default(client, monkeypatch):
    monkeypatch.delenv("EIS_DEMO_MODE", raising=False)
    monkeypatch.delenv("EIS_SIGNUP_ENABLED", raising=False)
    cfg = client.get("/config").json()
    assert cfg["demo_mode"] is True and cfg["signup_enabled"] is False

    monkeypatch.setenv("EIS_DEMO_MODE", "0")
    monkeypatch.setenv("EIS_SIGNUP_ENABLED", "1")
    cfg = client.get("/config").json()
    assert cfg["demo_mode"] is False and cfg["signup_enabled"] is True


def test_signup_is_refused_until_explicitly_enabled(client, monkeypatch):
    monkeypatch.delenv("EIS_SIGNUP_ENABLED", raising=False)
    body = {"phone_number": "0712345678", "latitude": -1.28, "longitude": 36.82}
    assert client.post("/subscribers", json=body).status_code == 503
    from src.database import get_connection

    conn = get_connection()
    assert conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0] == 0
    conn.close()


def test_citizen_app_gets_the_strict_csp(client):
    for path in ("/dashboard/", "/dashboard/index.html"):
        csp = client.get(path).headers["content-security-policy"]
        assert "script-src 'self'" in csp and "unsafe-inline" not in csp, path


def test_citizen_app_has_no_inline_code_and_never_uses_innerhtml():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert "<style" not in html and not re.search(r"\sstyle=", html)
    assert not re.search(r"\son[a-z]+=", html)
    assert "innerHTML" not in APP_JS and "insertAdjacentHTML" not in APP_JS and "document.write" not in APP_JS


def test_crime_and_missing_child_reports_are_never_stored_on_the_phone():
    # They send directly and never touch the outbox.
    for fn in ("sendCrime", "sendMissing"):
        src = _function_source(fn)
        assert "sendCitizenReport" not in src and "Outbox" not in src and "localStorage" not in src, fn


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_only_fire_flood_medical_may_wait_in_the_outbox():
    consts = re.search(r"const CRIME = new Set\(\[.*?\]\);\s*.*?const QUEUEABLE = new Set\(\[.*?\]\);", APP_JS, re.S).group(0)
    harness = "\n".join([consts, _function_source("shouldQueue"), """
        const types = ['fire', 'flood', 'medical_emergency', 'accident',
                       'robbery', 'burglary', 'theft', 'assault', 'vandalism', 'suspicious_activity',
                       'missing_child', 'something_new'];
        console.log(JSON.stringify(Object.fromEntries(types.map(t => [t, shouldQueue(t)]))));
    """])
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout)
    assert [t for t, q in result.items() if q] == ["fire", "flood", "medical_emergency", "accident"]


def test_app_never_promises_that_help_is_coming():
    text = (STATIC / "index.html").read_text(encoding="utf-8") + APP_JS
    for phrase in ("help is on the way", "help is coming", "ambulance is coming", "officers are coming"):
        assert phrase not in text.lower(), phrase


def test_demo_mode_refuses_missing_child_reports(client, monkeypatch):
    monkeypatch.setenv("EIS_DEMO_MODE", "1")

    body = {"child_name": "Test Child", "physical_description": "x", "last_seen_latitude": -1.28,
            "last_seen_longitude": 36.82, "last_seen_time": "2026-09-18 14:00", "reporter_phone": "+254700000000"}
    res = client.post("/missing-child/report", json=body)
    assert res.status_code == 403 and "demo" in res.json()["detail"]


def test_rate_limit_key_uses_cloudflare_header_only_from_the_local_tunnel(monkeypatch):
    from starlette.requests import Request

    from src.main import client_address

    def req(host, cf=None):
        headers = [(b"cf-connecting-ip", cf.encode())] if cf else []
        return Request({"type": "http", "client": (host, 1234), "headers": headers, "method": "GET", "path": "/"})

    monkeypatch.delenv("EIS_BEHIND_CLOUDFLARE_TUNNEL", raising=False)
    assert client_address(req("127.0.0.1", "41.90.1.2")) == "127.0.0.1"  # switch off: header ignored
    monkeypatch.setenv("EIS_BEHIND_CLOUDFLARE_TUNNEL", "1")
    assert client_address(req("127.0.0.1", "41.90.1.2")) == "41.90.1.2"
    assert client_address(req("203.0.113.9", "41.90.1.2")) == "203.0.113.9"  # forged from outside: ignored
    assert client_address(req("127.0.0.1")) == "127.0.0.1"
