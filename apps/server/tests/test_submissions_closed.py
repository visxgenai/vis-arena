"""Deadline switch: submissions can be closed with a clear message to the CLI."""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from vis_arena_server import storage
from vis_arena_server.db import connect, init_db
from vis_arena_server.settings import settings


@pytest.fixture(autouse=True)
def _clean(monkeypatch) -> None:
    init_db()
    with connect() as db:
        for table in ("jobs", "submissions", "users"):
            db.execute(f"delete from {table}")
    monkeypatch.setattr(storage, "presigned_put", lambda key: {"url": "https://u", "method": "PUT", "headers": {}, "expires_in": 1})


def _register(client: TestClient) -> dict[str, str]:
    email = f"user-{uuid.uuid4().hex[:10]}@example.com"
    res = client.post("/v1/auth/register", json={"email": email, "password": "password123", "name": email})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def test_uploads_allowed_while_open(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(settings, "submissions_closed", False)
    auth = _register(client)
    assert client.post("/v1/submissions/uploads", headers=auth, json={"name": "s"}).status_code == 200


def test_uploads_refused_with_a_clear_message_when_closed(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(settings, "submissions_closed", True)
    auth = _register(client)

    res = client.post("/v1/submissions/uploads", headers=auth, json={"name": "s"})
    assert res.status_code == 403
    detail = res.json()["detail"]
    assert "closed" in detail.lower() and "deadline" in detail.lower()


def test_finalize_refused_when_closed(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(settings, "submissions_closed", False)
    auth = _register(client)
    submission_id = client.post("/v1/submissions/uploads", headers=auth, json={"name": "s"}).json()["submission"]["id"]

    monkeypatch.setattr(settings, "submissions_closed", True)
    res = client.post(f"/v1/submissions/{submission_id}/finalize", headers=auth, json={})
    assert res.status_code == 403
    assert "closed" in res.json()["detail"].lower()


def test_reading_submissions_still_works_when_closed(client: TestClient, monkeypatch) -> None:
    # participants must still see their history, scores and reviews after the deadline
    monkeypatch.setattr(settings, "submissions_closed", True)
    auth = _register(client)
    assert client.get("/v1/submissions", headers=auth).status_code == 200
    assert client.get("/v1/me/peer-reviews", headers=auth).status_code == 200
