from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class User(BaseModel):
    id: str
    email: str
    name: str | None = None


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: User


class Dataset(BaseModel):
    id: str
    name: str
    visibility: str = "private"
    created_at: datetime | None = None
    task_count: int = 0


class Task(BaseModel):
    id: str
    dataset_id: str
    title: str
    version: int = 1
    metadata: dict[str, Any] = Field(default_factory=dict)


class Submission(BaseModel):
    id: str
    name: str
    status: str
    created_at: datetime | None = None
    score: float | None = None


class LLMToken(BaseModel):
    provider: str
    model: str
    access_token: str
    expires_at: datetime
    base_url: str | None = None


class LLMMessage(BaseModel):
    provider: str
    model: str
    message: dict[str, Any]
    usage: dict[str, int]
    remaining_submission_tokens: int
