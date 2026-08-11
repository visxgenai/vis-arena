"""Participants can read their own reviews per round — anonymized, comments included."""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from vis_arena_server.db import connect, init_db, now_iso


@pytest.fixture(autouse=True)
def _clean() -> None:
    init_db()
    with connect() as db:
        for table in ("llm_usage", "evaluations", "round_participants", "jobs", "review_rounds", "submissions", "users"):
            db.execute(f"delete from {table}")


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _register(client: TestClient) -> tuple[str, dict[str, str]]:
    email = f"{_id('user')}@example.com"
    res = client.post("/v1/auth/register", json={"email": email, "password": "password123", "name": email})
    assert res.status_code == 200, res.text
    body = res.json()
    return body["user"]["id"], {"Authorization": f"Bearer {body['access_token']}"}


def _insert_evaluation(db, *, round_id, artifact_job_id, evaluator_type, evaluator_user_id,
                       status="succeeded", score=None, result=None, carried=0, created_at=None):
    db.execute(
        "insert into evaluations (id, round_id, artifact_job_id, evaluator_type, evaluator_user_id, "
        "evaluator_submission_id, evaluator_name, job_id, status, score, result_json, "
        "is_carried_forward, created_at, updated_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (_id("eval"), round_id, artifact_job_id, evaluator_type, evaluator_user_id,
         _id("esub"), "SECRET-REVIEWER-NAME", _id("job"), status, score,
         json.dumps(result) if result else None, carried, created_at or now_iso(), now_iso()),
    )


def _seed_reviewed_round(owner_id: str) -> None:
    now = now_iso()
    submission_id = _id("sub")
    artifact_job_id = _id("gen")
    round_id = _id("round")
    with connect() as db:
        db.execute(
            "insert into submissions (id, owner_id, name, status, score, s3_key, finalized_at, created_at) "
            "values (?, ?, ?, ?, ?, ?, ?, ?)",
            (submission_id, owner_id, "mine", "succeeded", None, "s3", now, now),
        )
        db.execute(
            "insert into jobs (id, submission_id, job_type, generator_submission_id, dataset_id, task_id, "
            "status, preview_s3_key, created_at, updated_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (artifact_job_id, submission_id, "generation", submission_id, "ds", "task-a", "succeeded", "p", now, now),
        )
        db.execute(
            "insert into review_rounds (id, name, status, ends_at, created_at, updated_at) values (?, ?, ?, ?, ?, ?)",
            (round_id, "Round 9 - Public", "complete", now, now, now),
        )
        detail = {"summary": "nice chart, weak legend", "criteria": [{"id": "visual_craft", "score": 3, "evidence": "labels overlap"}]}
        _insert_evaluation(db, round_id="legacy", artifact_job_id=artifact_job_id, evaluator_type="self",
                           evaluator_user_id=owner_id, score=88.0, result=detail)
        _insert_evaluation(db, round_id=round_id, artifact_job_id=artifact_job_id, evaluator_type="peer",
                           evaluator_user_id=_id("peer1"), score=72.0, result=detail, created_at="2026-08-01T00:00:00+00:00")
        _insert_evaluation(db, round_id=round_id, artifact_job_id=artifact_job_id, evaluator_type="peer",
                           evaluator_user_id=_id("peer2"), score=64.0, result=detail, carried=1, created_at="2026-08-01T00:01:00+00:00")
        _insert_evaluation(db, round_id=round_id, artifact_job_id=artifact_job_id, evaluator_type="peer",
                           evaluator_user_id=_id("peer3"), status="failed", created_at="2026-08-01T00:02:00+00:00")
        _insert_evaluation(db, round_id="legacy", artifact_job_id=artifact_job_id, evaluator_type="central",
                           evaluator_user_id=_id("judge"), score=150.0, result={"summary": "PRIVATE-JUDGE-REPORT"})


def test_my_peer_reviews_lists_rounds_anonymized(client: TestClient) -> None:
    user_id, headers = _register(client)
    _seed_reviewed_round(user_id)

    res = client.get("/v1/me/peer-reviews", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["rounds"]) == 1
    round_item = body["rounds"][0]
    assert round_item["round_name"] == "Round 9 - Public"

    items = round_item["items"]
    labels = [item["reviewer"] for item in items]
    assert "Self" in labels
    assert "Peer reviewer A" in labels and "Peer reviewer B" in labels and "Peer reviewer C" in labels

    by_label = {item["reviewer"]: item for item in items}
    assert by_label["Peer reviewer A"]["score"] == 72.0
    assert by_label["Peer reviewer A"]["summary"] == "nice chart, weak legend"
    assert by_label["Peer reviewer A"]["criteria"][0]["evidence"] == "labels overlap"
    assert by_label["Peer reviewer B"]["carried_forward"] is True
    assert by_label["Peer reviewer C"]["status"] == "failed"
    assert by_label["Peer reviewer C"]["score"] is None
    assert by_label["Self"]["score"] == 88.0

    # no reviewer identity, no central-judge content anywhere in the payload
    dumped = json.dumps(body)
    assert "SECRET-REVIEWER-NAME" not in dumped
    assert "peer1" not in dumped and "peer2" not in dumped
    assert "PRIVATE-JUDGE-REPORT" not in dumped


def test_my_peer_reviews_requires_auth_and_scopes_to_owner(client: TestClient) -> None:
    user_id, _headers = _register(client)
    _seed_reviewed_round(user_id)
    other_id, other_headers = _register(client)

    assert client.get("/v1/me/peer-reviews").status_code == 401
    body = client.get("/v1/me/peer-reviews", headers=other_headers).json()
    assert body["rounds"] == []
