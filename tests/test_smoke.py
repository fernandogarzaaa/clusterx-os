from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _login():
    r = client.post(
        "/api/auth/login",
        json={"email": "admin@clusterx.local", "password": "ChangeMe123!"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_login_and_me():
    token = _login()
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["email"] == "admin@clusterx.local"


def test_login_page_renders():
    r = client.get("/login")
    assert r.status_code == 200
    assert "Sign in" in r.text
