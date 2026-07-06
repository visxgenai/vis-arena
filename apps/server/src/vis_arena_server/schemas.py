from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    name: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    id: str
    email: str
    name: str | None = None
    # Populated by GET /v1/me so the account page can show the daily quota; omitted (None) elsewhere.
    daily_submission_limit: int | None = None
    submissions_today: int | None = None
    remaining_submissions_today: int | None = None


class UpdateMeRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=200)


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class DatasetResponse(BaseModel):
    id: str
    name: str
    visibility: str
    created_at: datetime | None = None
    task_count: int = 0


class TaskResponse(BaseModel):
    id: str
    dataset_id: str
    title: str
    version: int
    metadata: dict[str, Any]


class SubmissionResponse(BaseModel):
    id: str
    name: str
    status: str
    created_at: datetime | None = None
    score: float | None = None


class LLMTokenRequest(BaseModel):
    provider: str
    model: str
    purpose: str = "generation"


class LLMTokenResponse(BaseModel):
    provider: str
    model: str
    access_token: str
    expires_at: datetime
    base_url: str | None = None


class LLMMessageRequest(BaseModel):
    job_id: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = Field(default_factory=list)
    tool_choice: str | dict[str, Any] | None = "auto"
    model: str | None = None
    purpose: str = "generation"
    max_tokens: int = 4096


class LLMMessageResponse(BaseModel):
    provider: str
    model: str
    message: dict[str, Any]
    usage: dict[str, int]
    remaining_submission_tokens: int
