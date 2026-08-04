"""QA central judge — grades an artifact by whether it answers factual questions.

MECHANISM (public): load a question set from questions.json, open the artifact,
answer each question FROM THE RENDERED PAGE ONLY, compare against expected
answers, and emit an aggregate-only report (no question text, no expected
answers). The real question set is private: it is injected as questions.json at
bundle-build time and never committed to the public repository — this directory
ships only questions.example.json.

Hooks (arena contract, see agent.md in the python template):
    info()                            -> dict
    generate(workdir)                 -> dict   (judges never generate)
    evaluate(workdir, artifact_url)   -> dict   (the QA grading report)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

CLOUD_MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
LOCAL_MODEL = "gpt-5.5"
DEFAULT_MODEL = CLOUD_MODEL if os.environ.get("VIS_ARENA_JOB_ID") else LOCAL_MODEL

MAX_MODEL_CALLS = 14

JUDGE_PROMPT = """You are a careful visualization reader. You will be given an
interactive data-visualization page and a list of questions.

Answer every question using ONLY what the rendered page communicates: visible
text, labels, charts, tooltips, and what appears after you interact (click,
filter, hover). Do NOT read page source, <script> contents, or embedded raw
data — if the visualization does not communicate the answer, reply "unknown".

Use the playwright tool (PYTHON, playwright.sync_api; the page URL is in the
VIS_ARENA_ARTIFACT_URL env var) to open and interact with the page. Extract
visible text with locators or document.body.innerText — never page.content().

When done, call finish with one answer per question id. Keep each answer short:
a name, a number, or a year — no sentences."""


# --------------------------------------------------------------------------
# Question loading + grading (pure functions; unit-tested in the public repo)
# --------------------------------------------------------------------------

def load_questions(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    questions = data["questions"]
    for q in questions:
        for field in ("id", "type", "question", "answer"):
            if field not in q:
                raise ValueError(f"question missing field {field!r}")
    return questions


def questions_path(base: Path) -> Path:
    real = base / "questions.json"
    return real if real.exists() else base / "questions.example.json"


def normalize_answer(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[\"'`.,;:!?()\[\]]", "", text)
    text = re.sub(r"^(the|a|an)\s+", "", text)
    return re.sub(r"\s+", " ", text)


def _numbers_in(text: str) -> list[str]:
    return re.findall(r"-?\d+(?:\.\d+)?", text)


def is_correct(question: dict[str, Any], given: Any) -> bool:
    normalized = normalize_answer(given)
    if not normalized or normalized == "unknown":
        return False
    accepted = {normalize_answer(question["answer"])} | {
        normalize_answer(v) for v in question.get("accept", [])
    }
    if question.get("type") in ("number", "year"):
        expected_nums = _numbers_in(str(question["answer"]))
        return bool(expected_nums) and _numbers_in(normalized) == expected_nums
    if normalized in accepted:
        return True
    # Tolerate short surrounding words ("it is ivy echos") but never bare substrings
    # of a longer different name.
    return any(acc and re.search(rf"(^|\s){re.escape(acc)}($|\s)", normalized) for acc in accepted)


def grade(questions: list[dict[str, Any]], answers: dict[str, Any]) -> dict[str, Any]:
    """Aggregate-only report: never includes question text or expected answers."""
    per_question = [
        {"id": q["id"], "correct": is_correct(q, answers.get(q["id"]))} for q in questions
    ]
    correct = sum(1 for row in per_question if row["correct"])
    total = len(questions)
    score = round(100.0 * correct / total, 1) if total else 0.0
    return {
        "score": score,
        "max_score": 100,
        "summary": f"Central judge: answered {correct} of {total} grounded questions correctly from the rendered artifact.",
        "per_question": per_question,
        "metadata": {"judge": "qa-judge", "n_questions": total},
    }


# --------------------------------------------------------------------------
# Arena hooks
# --------------------------------------------------------------------------

def info() -> dict[str, Any]:
    return {
        "name": "qa-judge",
        "version": "0.1.0",
        "commands": ["evaluate"],
        "providers": ["arena-cloud"],
        "notes": "Central judge: grades artifacts against a private grounded question set.",
    }


def generate(workdir: Path) -> dict[str, Any]:
    raise SystemExit("qa-judge is an evaluator only; it does not generate artifacts")


def evaluate(workdir: Path, artifact_url: str) -> dict[str, Any]:
    questions = load_questions(questions_path(Path(__file__).resolve().parent))
    answers = _collect_answers(questions, workdir, artifact_url)
    return grade(questions, answers)


# --------------------------------------------------------------------------
# Answer collection: bounded tool loop (playwright + finish)
# --------------------------------------------------------------------------

def _collect_answers(questions: list[dict[str, Any]], workdir: Path, artifact_url: str) -> dict[str, Any]:
    from llm_client import make_llm_client  # lazy: grading functions stay importable without SDK deps

    client = make_llm_client("evaluation")
    question_lines = "\n".join(f"- {q['id']}: {q['question']}" for q in questions)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": JUDGE_PROMPT},
        {"role": "user", "content": f"ARTIFACT_URL={artifact_url}\n\nQuestions:\n{question_lines}"},
    ]
    finish_schema = {
        "type": "object",
        "properties": {
            "answers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "answer": {"type": "string"}},
                    "required": ["id", "answer"],
                },
            }
        },
        "required": ["answers"],
    }
    tools = [
        {
            "type": "function",
            "function": {
                "name": "playwright",
                "description": "Run a PYTHON playwright.sync_api script against the artifact page and print findings.",
                "parameters": {
                    "type": "object",
                    "properties": {"script": {"type": "string"}},
                    "required": ["script"],
                },
            },
        },
        {"type": "function", "function": {"name": "finish", "description": "Submit one short answer per question id.", "parameters": finish_schema}},
    ]

    for call_index in range(1, MAX_MODEL_CALLS + 1):
        tool_choice: str | dict[str, Any] = "auto"
        if call_index == MAX_MODEL_CALLS:
            messages.append({"role": "user", "content": "FINAL CALL: call finish NOW with your best answer for every question id (use \"unknown\" where the page did not communicate it)."})
            tool_choice = {"type": "function", "function": {"name": "finish"}}
        message = client.create(model=DEFAULT_MODEL, messages=messages, tools=tools, tool_choice=tool_choice)
        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            messages.append({"role": "user", "content": "Use the playwright tool to inspect the page, or call finish with your answers."})
            continue
        for call in calls:
            function = call.get("function") or {}
            try:
                args = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError as exc:
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": f"Tool argument JSON error: {exc}"})
                continue
            if function.get("name") == "finish":
                return {row.get("id"): row.get("answer") for row in args.get("answers", [])}
            if function.get("name") == "playwright":
                output = _run_playwright(args.get("script") or "", workdir, artifact_url)
            else:
                output = f"Unknown tool: {function.get('name')}"
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": output[:12000]})
    return {}


_JS_HINTS = ("require(", "=>", "document.", "const ", "let ", "async function")


def _run_playwright(script: str, cwd: Path, artifact_url: str) -> str:
    if any(hint in script for hint in _JS_HINTS[:2]) or any(
        line.strip().startswith(("const ", "let ", "var ", "async function", "function "))
        for line in script.splitlines()
    ):
        return "Tool error: write a PYTHON playwright.sync_api script, not JavaScript."
    cwd.mkdir(parents=True, exist_ok=True)
    path = cwd / ".qa_judge_playwright.py"
    path.write_text(script, encoding="utf-8")
    env = dict(os.environ, VIS_ARENA_ARTIFACT_URL=artifact_url)
    try:
        completed = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(cwd),
            text=True,
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=240,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return "Tool error: playwright script exceeded 240s; use a faster, more targeted script."
    return f"exit={completed.returncode}\n{completed.stdout}"
