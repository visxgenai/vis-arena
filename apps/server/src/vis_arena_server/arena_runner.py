"""
Direct OpenAI arena runner — no Docker required.

Runs multiple LLM models on visualization tasks, has them peer-grade each
other's outputs, and stores results in the arena DB so arena_data.py can
serve real chart data.

Usage (inside the server venv):
    python -m vis_arena_server.arena_runner [--rounds N]
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openai import OpenAI

from .db import connect, init_db, now_iso

# ---------------------------------------------------------------------------
# Participant models  (id used as submission name, must be a real OpenAI ID)
# ---------------------------------------------------------------------------

ARENA_PARTICIPANTS: list[dict[str, str]] = [
    {"id": "gpt-4.1",      "name": "GPT-4.1",       "color": "#4A9EEA"},
    {"id": "gpt-4o",       "name": "GPT-4o",         "color": "#E8A838"},
    {"id": "gpt-4.1-mini", "name": "GPT-4.1-mini",   "color": "#E06040"},
    {"id": "gpt-4o-mini",  "name": "GPT-4o-mini",    "color": "#D46090"},
    {"id": "gpt-4.1-nano", "name": "GPT-4.1-nano",   "color": "#30C878"},
]

# The models that act as evaluators (subset of participants is fine)
EVALUATOR_IDS = ["gpt-4.1", "gpt-4o", "gpt-4o-mini"]

TASKS_DIR = Path(__file__).resolve().parents[4] / "examples" / "tasks"

GENERATION_SYSTEM = (
    "You are a web data-visualization expert. "
    "Given a task description and CSV data, produce a complete self-contained "
    "single-file HTML visualization (no external dependencies). "
    "Return ONLY valid HTML — no markdown fences, no explanations."
)

EVALUATION_SYSTEM = (
    "You are an impartial visualization evaluator. "
    "Evaluate the HTML submission against the task rubric. "
    "Respond with valid JSON only (no markdown): "
    '{"score": <0-100 integer>, "summary": "<one sentence>", '
    '"criteria": {"correctness": <0-35>, "usability": <0-25>, '
    '"visual_design": <0-20>, "robustness": <0-20>}}'
)

SYSTEM_USER_EMAIL = "arena-system@vis-arena.internal"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_arena(num_rounds: int = 4) -> None:
    """Run `num_rounds` arena rounds and persist results to the DB."""
    init_db()
    client = _make_client()

    task = _load_task()
    if task is None:
        print("No tasks found in examples/tasks — skipping arena run.", file=sys.stderr)
        return

    system_user_id = _ensure_system_user()
    participant_sub_ids = _ensure_participant_submissions(system_user_id)

    print(f"Running {num_rounds} arena rounds on task '{task['id']}'…")
    for round_num in range(1, num_rounds + 1):
        print(f"\n  Round {round_num}/{num_rounds}")
        _run_round(client, task, participant_sub_ids, system_user_id, round_num)

    print("\nArena run complete. Scores written to DB.")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_client() -> OpenAI:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return OpenAI(api_key=key)


def _load_task() -> dict[str, Any] | None:
    """Return the first task.md found under TASKS_DIR."""
    for task_md in TASKS_DIR.rglob("task.md"):
        text = task_md.read_text(encoding="utf-8")
        # Read data files in the same directory
        data_files: dict[str, str] = {}
        for f in task_md.parent.rglob("*"):
            if f.is_file() and f.suffix in {".csv", ".json", ".txt"}:
                data_files[f.name] = f.read_text(encoding="utf-8")
        return {
            "id": task_md.parent.name,
            "task_text": text,
            "data_files": data_files,
        }
    return None


def _ensure_system_user() -> str:
    with connect() as db:
        row = db.execute("SELECT id FROM users WHERE email = ?", (SYSTEM_USER_EMAIL,)).fetchone()
        if row:
            return row["id"]
        user_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO users (id, email, password_hash, name, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, SYSTEM_USER_EMAIL, "!", "Arena System", now_iso()),
        )
        return user_id


def _ensure_participant_submissions(owner_id: str) -> dict[str, str]:
    """Return {model_id: submission_id}, creating submissions if needed."""
    result: dict[str, str] = {}
    with connect() as db:
        for p in ARENA_PARTICIPANTS:
            row = db.execute(
                "SELECT id FROM submissions WHERE owner_id = ? AND name = ?",
                (owner_id, p["id"]),
            ).fetchone()
            if row:
                result[p["id"]] = row["id"]
            else:
                sub_id = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO submissions (id, owner_id, name, status, score, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (sub_id, owner_id, p["id"], "arena", None, now_iso()),
                )
                result[p["id"]] = sub_id
    return result


def _run_round(
    client: OpenAI,
    task: dict[str, Any],
    participant_sub_ids: dict[str, str],
    system_user_id: str,
    round_num: int,
) -> None:
    """Generate visualizations, evaluate them, and persist results."""
    generated: dict[str, str] = {}  # model_id -> html

    # ---- Generation phase ----
    for p in ARENA_PARTICIPANTS:
        mid = p["id"]
        print(f"    Generating with {mid}…", end=" ", flush=True)
        html = _generate(client, mid, task)
        generated[mid] = html
        print("done")

    # ---- Evaluation phase (peer-grading) ----
    # scores[generator_id][evaluator_id] = score
    scores: dict[str, dict[str, float]] = {p["id"]: {} for p in ARENA_PARTICIPANTS}

    for eval_id in EVALUATOR_IDS:
        for gen_id, html in generated.items():
            if eval_id == gen_id:
                continue  # don't self-evaluate
            print(f"    {eval_id} evaluates {gen_id}…", end=" ", flush=True)
            score = _evaluate(client, eval_id, task, html)
            scores[gen_id][eval_id] = score
            print(f"{score:.1f}")

    # ---- Persist to DB ----
    now = now_iso()
    with connect() as db:
        for p in ARENA_PARTICIPANTS:
            gen_id = p["id"]
            sub_id = participant_sub_ids[gen_id]
            peer_scores = list(scores[gen_id].values())
            if not peer_scores:
                continue
            avg_score = sum(peer_scores) / len(peer_scores)
            evaluators_json = json.dumps(scores[gen_id])

            job_id = str(uuid.uuid4())
            result = {
                "score": round(avg_score, 2),
                "peer_scores": scores[gen_id],
                "round": round_num,
                "task_id": task["id"],
                "html_length": len(generated[gen_id]),
            }
            db.execute(
                """INSERT INTO jobs
                   (id, submission_id, task_id, status, result_json,
                    arena_round, arena_evaluators, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id, sub_id, task["id"], "succeeded",
                    json.dumps(result),
                    round_num, evaluators_json,
                    now, now,
                ),
            )
            # Update submission's running average score
            db.execute(
                "UPDATE submissions SET score = ?, status = 'succeeded' WHERE id = ?",
                (round(avg_score, 2), sub_id),
            )


def _generate(client: OpenAI, model_id: str, task: dict[str, Any]) -> str:
    """Call model to generate an HTML visualization."""
    data_block = "\n\n".join(
        f"=== {fname} ===\n{content}" for fname, content in task["data_files"].items()
    )
    user_prompt = f"Task:\n{task['task_text']}\n\nData:\n{data_block}"
    try:
        resp = client.chat.completions.create(
            model=model_id,
            messages=[
                {"role": "system", "content": GENERATION_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=4096,
            temperature=0.7,
        )
        return resp.choices[0].message.content or "<html><body>No output</body></html>"
    except Exception as exc:
        print(f"\n      [WARN] {model_id} generation failed: {exc}", file=sys.stderr)
        return f"<html><body>Generation failed: {exc}</body></html>"


def _evaluate(client: OpenAI, evaluator_id: str, task: dict[str, Any], html: str) -> float:
    """Have evaluator_id score the HTML submission. Returns 0–100."""
    prompt = (
        f"Task description:\n{task['task_text']}\n\n"
        f"HTML submission (first 6000 chars):\n{html[:6000]}\n\n"
        "Evaluate and return JSON."
    )
    try:
        resp = client.chat.completions.create(
            model=evaluator_id,
            messages=[
                {"role": "system", "content": EVALUATION_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=512,
            temperature=0.2,
        )
        content = resp.choices[0].message.content or "{}"
        # Strip markdown fences if present
        content = content.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        data = json.loads(content)
        return float(data.get("score", 50))
    except Exception as exc:
        print(f"\n      [WARN] {evaluator_id} evaluation failed: {exc}", file=sys.stderr)
        return 50.0  # neutral fallback


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def run() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run Vis Arena evaluation rounds")
    parser.add_argument("--rounds", type=int, default=4, help="Number of rounds to run")
    args = parser.parse_args()

    # Load .env if present
    env_file = Path(__file__).resolve().parents[4] / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    run_arena(num_rounds=args.rounds)


if __name__ == "__main__":
    run()
