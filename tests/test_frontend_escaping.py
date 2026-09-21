"""
Regression test for a real stored-XSS hole in the dashboard: reporter-
controlled text (incident `area`, missing-child `child_name`, ...) was
interpolated into innerHTML templates unescaped, so a public report with
<img onerror=...> ran script in every visitor's browser - including officers,
whose login token lives in sessionStorage.

This runs the dashboard's ACTUAL rendering functions (extracted from
static/index.html) in Node against hostile input and asserts no live markup
comes out. Skipped if Node isn't installed.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parent.parent / "static" / "index.html"
PAYLOAD = '<img src=x onerror="window.__pwned=1"><script>alert(1)</script>"\'&'


def _function_source(script: str, name: str) -> str:
    m = re.search(rf"(?:async )?function {name}\([^)]*\) \{{.*?\n\}}", script, re.S)
    assert m, f"could not find function {name} in static/index.html"
    return m.group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_dashboard_escapes_reporter_controlled_text():
    html = INDEX.read_text(encoding="utf-8")
    script = re.search(r"<script>(.*)</script>\s*</body>", html, re.S).group(1)

    harness = "\n".join([
        _function_source(script, "esc"),
        _function_source(script, "renderIncidentRow"),
        _function_source(script, "renderMissingCase"),
        f"const P = {json.dumps(PAYLOAD)};",
        """
        const inc = { id: 1, type: P, area: P, predicted_severity: P, scoring_stage: 'strategic',
                      confidence_score: 0.5, alert_tier: P, source: P, visibility: 'public' };
        const mc = { id: 2, child_name: P, age: 8, physical_description: P, clothing_description: P,
                     last_seen_area: P, last_seen_time: P, reporter_phone: P, reporter_relationship: P,
                     status: 'verified' };
        console.log(JSON.stringify([renderIncidentRow(inc), renderMissingCase(mc, true), renderMissingCase(mc, false)]));
        """,
    ])
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr

    for rendered in json.loads(out.stdout):
        assert "<img" not in rendered and "<script" not in rendered, rendered
        assert "onerror=\"" not in rendered, rendered
        assert "&lt;img" in rendered, "payload should be shown as text, escaped"
