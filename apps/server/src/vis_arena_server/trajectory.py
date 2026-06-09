from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .db import now_iso
from .settings import settings


def normalize_phase(value: str | None) -> str:
    phase = (value or "").strip().lower()
    if phase.startswith("eval") or "review" in phase:
        return "evaluation"
    return "generation"


def broker_trajectory_path(job_id: str, phase_or_purpose: str | None) -> Path:
    safe_job_id = re.sub(r"[^A-Za-z0-9_.-]", "_", job_id)
    return settings.storage_dir / "trajectories" / "jobs" / safe_job_id / f"{normalize_phase(phase_or_purpose)}.jsonl"


def append_broker_event(job_id: str, phase_or_purpose: str | None, event: dict[str, Any], *, dedupe_key: str | None = None) -> None:
    if not settings.record_trajectory:
        return
    path = broker_trajectory_path(job_id, phase_or_purpose)
    path.parent.mkdir(parents=True, exist_ok=True)
    if dedupe_key and _seen(path, dedupe_key):
        return
    payload = {
        "type": event["type"],
        "source": "llm_broker",
        "job_id": job_id,
        "phase": normalize_phase(phase_or_purpose),
        "timestamp": now_iso(),
        **event,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n")
    if dedupe_key:
        _mark_seen(path, dedupe_key)


def stable_event_key(*parts: Any) -> str:
    body = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _seen(path: Path, key: str) -> bool:
    seen_path = path.with_suffix(path.suffix + ".seen")
    if not seen_path.exists():
        return False
    return key in set(seen_path.read_text(encoding="utf-8").splitlines())


def _mark_seen(path: Path, key: str) -> None:
    seen_path = path.with_suffix(path.suffix + ".seen")
    with seen_path.open("a", encoding="utf-8") as handle:
        handle.write(key + "\n")
