from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import bcrypt
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .db import connect, now_iso, row_to_dict
from .settings import settings

bearer = HTTPBearer()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(days=30),
        "scope": "arena"
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

