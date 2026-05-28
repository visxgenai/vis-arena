"""End-to-end smoke for the routes the evaluation-server-frontend BFF hits.

Mirrors what the Next.js shell does on first sign-up:
    POST /v1/auth/register
    POST /v1/auth/login
    GET  /v1/me
    GET  /v1/submissions   (empty list for a fresh user)

Run from repo root:
    pytest apps/server/tests
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient


def _unique_email() -> str:
    # Unique per-call so multiple test runs against the same temp DB don't collide.
    return f"smoke-{uuid.uuid4().hex[:12]}@example.com"


def test_register_login_me_submissions(client: TestClient) -> None:
    email = _unique_email()
    password = "correct-horse-battery-staple"
    name = "Smoke Test"

    # --- register ---
    res = client.post(
        "/v1/auth/register",
        json={"email": email, "password": password, "name": name},
    )
    assert res.status_code == 200, res.text
    register_body = res.json()
    assert register_body["token_type"] == "bearer"
    assert register_body["access_token"]
    assert register_body["user"]["email"] == email
    assert register_body["user"]["name"] == name

    # --- duplicate register should fail with the message the BFF surfaces ---
    res = client.post(
        "/v1/auth/register",
        json={"email": email, "password": password, "name": name},
    )
    assert res.status_code == 400
    assert "already registered" in res.json()["detail"].lower()

    # --- login ---
    res = client.post(
        "/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert res.status_code == 200, res.text
    login_body = res.json()
    assert login_body["access_token"]
    assert login_body["user"]["email"] == email

    token = login_body["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    # --- /v1/me ---
    res = client.get("/v1/me", headers=auth)
    assert res.status_code == 200, res.text
    me = res.json()
    assert me["email"] == email
    assert me["name"] == name

    # --- /v1/submissions (empty for a brand-new user) ---
    res = client.get("/v1/submissions", headers=auth)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body == {"items": []}


def test_login_with_wrong_password_returns_401(client: TestClient) -> None:
    email = _unique_email()
    client.post(
        "/v1/auth/register",
        json={"email": email, "password": "rightpw", "name": "X"},
    )
    res = client.post(
        "/v1/auth/login",
        json={"email": email, "password": "wrongpw"},
    )
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid email or password"


def test_protected_route_without_token_returns_401(client: TestClient) -> None:
    res = client.get("/v1/submissions")
    # FastAPI's HTTPBearer dependency returns 403 by default when the header
    # is missing, which is what the frontend's BFF treats as "unauthorized".
    assert res.status_code in (401, 403)


def test_health(client: TestClient) -> None:
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}
