from __future__ import annotations

import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx
from pydantic import TypeAdapter

from .models import AuthResponse, Dataset, LLMMessage, LLMToken, Submission, Task


_EXCLUDED_BUNDLE_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
}
_EXCLUDED_BUNDLE_FILES = {".DS_Store", ".env"}


class VisArenaError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class VisArenaClient:
    def __init__(self, base_url: str = "https://visagent.org", token: str | None = None, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout, follow_redirects=True, headers=self._headers(), trust_env=False)

    def close(self) -> None:
        self._client.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise VisArenaError(f"{response.status_code}: {response.text[:500]}", response.status_code)
        return response

    def register(self, email: str, password: str, name: str | None = None) -> AuthResponse:
        response = self._request("POST", "/v1/auth/register", json={"email": email, "password": password, "name": name})
        auth = AuthResponse.model_validate(response.json())
        self.token = auth.access_token
        self._client.headers.update(self._headers())
        return auth

    def login(self, email: str, password: str) -> AuthResponse:
        response = self._request("POST", "/v1/auth/login", json={"email": email, "password": password})
        auth = AuthResponse.model_validate(response.json())
        self.token = auth.access_token
        self._client.headers.update(self._headers())
        return auth

    def me(self) -> dict[str, Any]:
        return self._request("GET", "/v1/me").json()

    def update_me(self, name: str) -> dict[str, Any]:
        return self._request("PATCH", "/v1/me", json={"name": name}).json()

    def version(self) -> dict[str, Any]:
        return self._request("GET", "/v1/version").json()

    def list_datasets(self) -> list[Dataset]:
        response = self._request("GET", "/v1/datasets")
        return TypeAdapter(list[Dataset]).validate_python(response.json()["items"])

    def resolve_dataset(self, value: str) -> Dataset:
        datasets = self.list_datasets()
        for dataset in datasets:
            if dataset.id == value:
                return dataset
        matches = [dataset for dataset in datasets if dataset.name.lower() == value.lower()]
        if not matches:
            lookup = _dataset_lookup_key(value)
            matches = [
                dataset
                for dataset in datasets
                if lookup
                and (
                    lookup == _dataset_lookup_key(dataset.name)
                    or lookup in _dataset_lookup_key(dataset.name)
                )
            ]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            available = ", ".join(sorted(f"{dataset.name} ({dataset.id})" for dataset in datasets)) or "(none)"
            raise VisArenaError(f"No dataset named or with id '{value}'. Available: {available}")
        raise VisArenaError(
            f"Multiple datasets match '{value}'; pass the exact dataset id."
        )

    def upload_dataset(self, bundle_path: str | Path, name: str, visibility: str = "private") -> Dataset:
        with _as_zip(bundle_path) as path, path.open("rb") as handle:
            response = self._request("POST", "/v1/datasets/uploads", json={"name": name, "visibility": visibility})
            payload = response.json()
            upload = payload["upload"]
            put = _put_presigned_upload(upload["url"], handle, upload.get("headers", {}), self.timeout)
            response = self._request("POST", f"/v1/datasets/{payload['dataset']['id']}/finalize")
        return Dataset.model_validate(response.json())

    def list_tasks(self, dataset_id: str) -> list[Task]:
        response = self._request("GET", f"/v1/datasets/{dataset_id}/tasks")
        return TypeAdapter(list[Task]).validate_python(response.json()["items"])

    def download_dataset(self, dataset_id: str, output: str | Path) -> Path:
        signed = self._request("GET", f"/v1/datasets/{dataset_id}/download").json()
        response = httpx.get(signed["url"], timeout=self.timeout, follow_redirects=True, trust_env=False)
        if response.status_code >= 400:
            raise VisArenaError(f"S3 download failed: {response.status_code}: {response.text[:500]}", response.status_code)
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(response.content)
        return output_path

    def upload_submission(self, bundle_path: str | Path, name: str, dataset_id: str | None = None) -> Submission:
        with _as_zip(bundle_path) as path, path.open("rb") as handle:
            response = self._request("POST", "/v1/submissions/uploads", json={"name": name})
            payload = response.json()
            upload = payload["upload"]
            put = _put_presigned_upload(upload["url"], handle, upload.get("headers", {}), self.timeout)
            response = self._request("POST", f"/v1/submissions/{payload['submission']['id']}/finalize", json={"dataset_id": dataset_id})
        return Submission.model_validate(response.json())

    def list_submissions(self) -> list[Submission]:
        response = self._request("GET", "/v1/submissions")
        return TypeAdapter(list[Submission]).validate_python(response.json()["items"])

    def get_submission(self, submission_id: str) -> Submission:
        response = self._request("GET", f"/v1/submissions/{submission_id}")
        return Submission.model_validate(response.json())

    def get_submission_llm_usage(self, submission_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/submissions/{submission_id}/llm-usage").json()

    def list_submission_jobs(self, submission_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/submissions/{submission_id}/jobs").json()["items"]

    def get_job_preview_url(self, job_id: str) -> str:
        return str(self._request("GET", f"/v1/jobs/{job_id}/preview-url").json()["url"])

    def get_job_evaluation_report(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/jobs/{job_id}/evaluation-report").json()

    def wait_for_submission(self, submission_id: str, poll_seconds: float = 5.0, timeout_seconds: float = 900.0) -> Submission:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            submission = self.get_submission(submission_id)
            if submission.status in {"succeeded", "failed", "cancelled"}:
                return submission
            time.sleep(poll_seconds)
        raise VisArenaError(f"Timed out waiting for submission {submission_id}")

    def request_llm_token(self, provider: str, model: str, purpose: str = "generation") -> LLMToken:
        response = self._request("POST", "/v1/llm/token", json={"provider": provider, "model": model, "purpose": purpose})
        return LLMToken.model_validate(response.json())

    def create_llm_message(
        self,
        *,
        job_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = "auto",
        model: str | None = None,
        purpose: str = "generation",
        max_tokens: int = 4096,
    ) -> LLMMessage:
        response = self._request(
            "POST",
            "/v1/llm/messages",
            json={
                "job_id": job_id,
                "messages": messages,
                "tools": tools or [],
                "tool_choice": tool_choice,
                "model": model,
                "purpose": purpose,
                "max_tokens": max_tokens,
            },
        )
        return LLMMessage.model_validate(response.json())

    def list_rounds(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/peer-reviews/rounds", params={"limit": limit}).json()["items"]

    def open_round(
        self,
        *,
        name: str,
        starts_at: str | None = None,
        ends_at: str | None = None,
        interval_seconds: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": name}
        if starts_at:
            payload["starts_at"] = starts_at
        if ends_at:
            payload["ends_at"] = ends_at
        if interval_seconds:
            payload["interval_seconds"] = interval_seconds
        return self._request("POST", "/v1/peer-reviews/rounds", json=payload).json()

    def get_round(self, round_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/peer-reviews/rounds/{round_id}").json()

    def close_round(self, round_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/peer-reviews/rounds/{round_id}/close").json()

    def start_peer_review_round(self, round_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/peer-reviews/rounds/{round_id}/start-peer-review").json()

    def round_leaderboard(self, round_id: str, limit: int = 100) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/peer-reviews/rounds/{round_id}/leaderboard", params={"limit": limit}).json()["items"]


@contextmanager
def _as_zip(path_like: str | Path):
    path = Path(path_like)
    if path.is_file():
        yield path
        return
    if not path.is_dir():
        raise VisArenaError(f"Path does not exist: {path}")
    with TemporaryDirectory() as tmp:
        archive_path = Path(tmp) / f"{path.name}.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for child in path.rglob("*"):
                relative = child.relative_to(path)
                if _is_excluded_bundle_path(relative):
                    continue
                if child.is_file():
                    archive.write(child, relative)
        yield archive_path


def _put_presigned_upload(url: str, handle, headers: dict[str, str], timeout: float) -> httpx.Response:
    response = httpx.put(url, content=handle, headers=headers, timeout=timeout, follow_redirects=False, trust_env=False)
    if 300 <= response.status_code < 400 and response.headers.get("location"):
        raise VisArenaError(
            "S3 upload was redirected before storing the object. "
            "Check that VIS_ARENA_S3_REGION matches the bucket region.",
            response.status_code,
        )
    if not 200 <= response.status_code < 300:
        raise VisArenaError(f"S3 upload failed: {response.status_code}: {response.text[:500]}", response.status_code)
    return response


def _is_excluded_bundle_path(relative: Path) -> bool:
    return any(part in _EXCLUDED_BUNDLE_DIRS for part in relative.parts) or relative.name in _EXCLUDED_BUNDLE_FILES


def _dataset_lookup_key(value: str) -> str:
    return "".join(char for char in value.lower() if char.isalnum())
