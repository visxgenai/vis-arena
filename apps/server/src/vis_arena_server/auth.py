from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext

from .db import connect, now_iso, row_to_dict
from .settings import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer = HTTPBearer()


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


# Login/CLI session tokens must outlast the event window (challenge runs to Aug 31),
# else early registrants get logged out mid-event. The per-job token (create_token
# default) stays short — it only needs the job's runtime.
LOGIN_TOKEN_DAYS = 120


def create_token(user_id: str, expires_days: int = 30) -> str:
    payload = {
        "sub": user_id,
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(days=expires_days),
        "scope": "arena"
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def create_job_token(job_id: str) -> str:
    payload = {
        "sub": job_id,
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(hours=12),
        "scope": "runner-job",
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def create_user(email: str, password: str, name: str | None) -> dict:
    user_id = str(uuid.uuid4())
    with connect() as db:
        db.execute(
            "insert into users (id, email, password_hash, name, created_at) values (?, ?, ?, ?, ?)",
            (user_id, email.lower(), hash_password(password), name, now_iso()),
        )
        row = db.execute("select id, email, name, created_at from users where id = ?", (user_id,)).fetchone()
    return dict(row)


def authenticate(email: str, password: str) -> dict | None:
    with connect() as db:
        row = db.execute("select * from users where email = ?", (email.lower(),)).fetchone()
    if row is None or not verify_password(password, row["password_hash"]):
        return None
    return {"id": row["id"], "email": row["email"], "name": row["name"], "created_at": row["created_at"]}


def update_user_name(user_id: str, name: str) -> dict:
    display_name = name.strip()
    if not display_name:
        raise HTTPException(status_code=422, detail="Display name cannot be empty")
    with connect() as db:
        db.execute("update users set name = ? where id = ?", (display_name, user_id))
        row = db.execute("select id, email, name, created_at from users where id = ?", (user_id,)).fetchone()
    user = row_to_dict(row)
    if not user:
        raise HTTPException(status_code=401, detail="Unknown user")
    return user


def update_user_password(user_id: str, current_password: str, new_password: str) -> None:
    if len(new_password) < 8:
        raise HTTPException(status_code=422, detail="New password must be at least 8 characters")
    with connect() as db:
        row = db.execute("select password_hash from users where id = ?", (user_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=401, detail="Unknown user")
        if not verify_password(current_password, row["password_hash"]):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        db.execute("update users set password_hash = ? where id = ?", (hash_password(new_password), user_id))


def current_user(credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)]) -> dict:
    try:
        payload = jwt.decode(credentials.credentials, settings.secret_key, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc
    user_id = payload.get("sub")
    with connect() as db:
        row = db.execute("select id, email, name, created_at from users where id = ?", (user_id,)).fetchone()
    user = row_to_dict(row)
    if not user:
        raise HTTPException(status_code=401, detail="Unknown user")
    return user
