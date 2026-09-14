import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """
    Isolated DB per test - points src.database.DB_PATH at a throwaway file
    instead of the real eis.db. Tests must never touch real dev/pilot data;
    a broad cleanup query already wiped the real dev DB once during
    development (see docs/architecture.md / session notes) and that mistake
    shouldn't be repeatable from the test suite.
    """
    import src.database as database

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")

    from fastapi.testclient import TestClient

    from src.main import app

    # slowapi's limiter state is process-global (attached to the shared
    # `app` object), so without a reset, rate-limit counts would leak
    # between tests that happen to run in the same process.
    app.state.limiter.reset()

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def officer_token(client):
    from src.auth import hash_password
    from src.database import get_connection

    conn = get_connection()
    conn.execute(
        "INSERT INTO officers (username, hashed_password) VALUES (?, ?)",
        ("test_officer", hash_password("test_pw")),
    )
    conn.commit()
    conn.close()

    res = client.post("/token", data={"username": "test_officer", "password": "test_pw"})
    assert res.status_code == 200, res.text
    return res.json()["access_token"]
