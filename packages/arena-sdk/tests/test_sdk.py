"""Programmatic SDK tests (Tier 1).

Exercises VisArenaClient against the in-process FastAPI app.
Proves the HTTP contract between SDK and backend stays in sync.
"""

from __future__ import annotations

import uuid

import pytest

from vis_arena_sdk.client import VisArenaClient, VisArenaError


def _unique_email() -> str:
    return f"sdk-{uuid.uuid4().hex[:12]}@example.com"


def test_register_returns_auth_and_attaches_token(sdk_client: VisArenaClient) -> None:
    email = _unique_email()
    auth = sdk_client.register(email, "hunter2hunter2", "SDK Test")

    assert auth.access_token
    assert auth.user.email == email
    assert auth.user.name == "SDK Test"
    # Token should be attached for subsequent calls.
    assert sdk_client.token == auth.access_token
    assert sdk_client._client.headers["Authorization"] == f"Bearer {auth.access_token}"


def test_login_round_trip_and_me(sdk_client: VisArenaClient) -> None:
    email = _unique_email()
    sdk_client.register(email, "pw1234567890", "Round Trip")

    fresh = VisArenaClient(base_url=sdk_client.base_url)
    try:
        auth = fresh.login(email, "pw1234567890")
        assert auth.user.email == email

        me = fresh.me()
        assert me["email"] == email
        assert me["name"] == "Round Trip"
    finally:
        fresh.close()


def test_list_submissions_empty_for_new_user(sdk_client: VisArenaClient) -> None:
    sdk_client.register(_unique_email(), "anotherpw1234", None)
    assert sdk_client.list_submissions() == []


def test_wrong_password_raises_visarenaerror_401(sdk_client: VisArenaClient) -> None:
    email = _unique_email()
    sdk_client.register(email, "correctpassword", None)

    fresh = VisArenaClient(base_url=sdk_client.base_url)
    try:
        with pytest.raises(VisArenaError) as excinfo:
            fresh.login(email, "wrongpassword")
        assert excinfo.value.status_code == 401
    finally:
        fresh.close()


def test_missing_token_on_protected_route_raises(sdk_client: VisArenaClient) -> None:
    # sdk_client has no token yet — calling /v1/me should 401 or 403.
    with pytest.raises(VisArenaError) as excinfo:
        sdk_client.me()
    assert excinfo.value.status_code in (401, 403)


def test_duplicate_register_raises_400(sdk_client: VisArenaClient) -> None:
    email = _unique_email()
    sdk_client.register(email, "passwordone", None)

    fresh = VisArenaClient(base_url=sdk_client.base_url)
    try:
        with pytest.raises(VisArenaError) as excinfo:
            fresh.register(email, "passwordone", None)
        assert excinfo.value.status_code == 400
    finally:
        fresh.close()


from vis_arena_sdk.models import Dataset


def test_resolve_dataset_by_name_case_insensitive(sdk_client, monkeypatch) -> None:
    datasets = [Dataset(id="uuid-1", name="monthly-sales", visibility="public", task_count=1)]
    monkeypatch.setattr(sdk_client, "list_datasets", lambda: datasets)

    assert sdk_client.resolve_dataset("Monthly-Sales").id == "uuid-1"


def test_resolve_dataset_by_slug_alias(sdk_client, monkeypatch) -> None:
    datasets = [
        Dataset(id="uuid-1", name="Monthly Sales Dashboard", visibility="public", task_count=1),
        Dataset(id="uuid-2", name="IEEE VIS Publications Explorer", visibility="public", task_count=1),
    ]
    monkeypatch.setattr(sdk_client, "list_datasets", lambda: datasets)

    assert sdk_client.resolve_dataset("monthly-sales").id == "uuid-1"
    assert sdk_client.resolve_dataset("ieee-vis-publications").id == "uuid-2"


def test_resolve_dataset_by_id(sdk_client, monkeypatch) -> None:
    datasets = [Dataset(id="uuid-1", name="monthly-sales", visibility="public", task_count=1)]
    monkeypatch.setattr(sdk_client, "list_datasets", lambda: datasets)

    assert sdk_client.resolve_dataset("uuid-1").name == "monthly-sales"


def test_resolve_dataset_no_match_lists_available(sdk_client, monkeypatch) -> None:
    datasets = [Dataset(id="uuid-1", name="monthly-sales", visibility="public", task_count=1)]
    monkeypatch.setattr(sdk_client, "list_datasets", lambda: datasets)

    with pytest.raises(VisArenaError) as exc:
        sdk_client.resolve_dataset("does-not-exist")
    assert "monthly-sales" in str(exc.value)


def test_resolve_dataset_ambiguous_name_raises(sdk_client, monkeypatch) -> None:
    datasets = [
        Dataset(id="uuid-1", name="dup a", visibility="public", task_count=1),
        Dataset(id="uuid-2", name="dup b", visibility="public", task_count=1),
    ]
    monkeypatch.setattr(sdk_client, "list_datasets", lambda: datasets)

    with pytest.raises(VisArenaError) as exc:
        sdk_client.resolve_dataset("dup")
    assert "Multiple datasets" in str(exc.value)
