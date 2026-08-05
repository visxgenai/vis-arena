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

# Effectively uncapped: the judge explores as long as it needs; the real stop is the
# per-job token budget. When remaining tokens run low (or at the generous call
# backstop) the loop forces a final answer so a score is ALWAYS produced.
MAX_MODEL_CALLS = 60
MIN_REMAINING_TOKENS = 60_000

# The judge scores two aspects from ONE exploration pass:
#   1. QA — does the visualization surface correct answers to grounded questions?
#   2. Rubric — the arena's five public criteria (storytelling quality).
RUBRIC_IDS = ("data_fidelity", "insightfulness", "narrative_coherence", "visual_craft", "functionality")

JUDGE_PROMPT = """You are a careful, impartial visualization judge. You will be
given an interactive data-visualization page and a list of factual questions.

You judge TWO aspects in one inspection:

ASPECT 1 — Questions. Answer every question using ONLY what the rendered page
communicates: visible text, labels, charts, tooltips, and what appears after
you interact (click, filter, hover). An answer only counts if it is supported
by VISUAL evidence (a chart, labelled mark, or interaction result) — prose-only
answer text with no supporting visualization does not count. Do NOT read page
source, <script> contents, or embedded raw data — if the visualization does not
communicate the answer, reply "unknown".

ASPECT 2 — Rubric. Rate each criterion 1-5:
- data_fidelity: displayed values/totals/trends internally consistent and
  plausible for the task. 1 fabricated/contradictory - 5 fully faithful.
- insightfulness: goes beyond plotting to trends, exceptions, implications.
  1 raw chart - 5 rich and decision-pointing.
- narrative_coherence: story arc (hook - build - payoff), consistent encodings.
  1 no story - 5 tight arc, every panel reinforces the payoff.
- visual_craft: chart choice, encodings, axes, labels, legibility, disclosure
  of filters/scope. 1 misrepresents/illegible - 5 optimal and accessible.
- functionality: interactive controls you ACTUALLY exercised work. 1 broken -
  5 all work and meaningfully aid analysis.

Use the playwright tool (PYTHON, playwright.sync_api; the page URL is in the
VIS_ARENA_ARTIFACT_URL env var) to open and interact with the page. Extract
visible text with locators or document.body.innerText — never page.content().

When done, call finish with one short answer per question id (a name, a number,
or a year — no sentences) AND the five rubric ratings with one-line evidence."""


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
    """QA aspect, aggregate-only: never includes question text or expected answers."""
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


def rubric_score(rubric: list[dict[str, Any]] | None) -> float | None:
    """Five-criteria rubric aspect: sum of clamped 1-5 levels x4 (template scale, 20-100).

    Returns None unless ALL five criteria carry an integer score — a partial
    rubric must not silently skew the combined score.
    """
    by_id = {str(r.get("id")): r for r in rubric or []}
    if set(by_id) != set(RUBRIC_IDS):
        return None
    total = 0
    for rid in RUBRIC_IDS:
        try:
            level = int(by_id[rid].get("score"))
        except (TypeError, ValueError):
            return None
        total += max(1, min(5, level))
    return round(total * 4.0, 1)


def grade_combined(
    questions: list[dict[str, Any]],
    answers: dict[str, Any],
    rubric: list[dict[str, Any]] | None,
    combine: str = "sum",
    qa_weight: float = 0.5,
) -> dict[str, Any]:
    """Two-aspect report: QA correctness + storytelling rubric.

    combine="sum" (default): the two 0-100 aspects add directly (max 200).
    combine="weighted": qa_weight x QA + (1 - qa_weight) x rubric (max 100) —
    kept as the adjustable alternative.
    Rubric criteria/evidence are public information (the arena's own template
    rubric); the QA aspect stays aggregate-only.
    """
    qa = grade(questions, answers)
    rscore = rubric_score(rubric)
    max_score = 100
    if rscore is None:
        combined = qa["score"]
        blend = "QA only — rubric incomplete"
    elif combine == "weighted":
        combined = round(qa_weight * qa["score"] + (1.0 - qa_weight) * rscore, 1)
        blend = f"{round(qa_weight * 100)}% QA + {round((1.0 - qa_weight) * 100)}% rubric"
    else:
        combined = round(qa["score"] + rscore, 1)
        max_score = 200
        blend = "QA + rubric, direct sum"
    criteria = []
    if rscore is not None:
        by_id = {str(r.get("id")): r for r in rubric or []}
        criteria = [
            {
                "id": rid,
                "score": max(1, min(5, int(by_id[rid].get("score")))),
                "max_score": 5,
                "evidence": str(by_id[rid].get("evidence") or "")[:300],
            }
            for rid in RUBRIC_IDS
        ]
    return {
        "score": combined,
        "max_score": max_score,
        "summary": (
            f"Central judge ({blend}): QA {qa['score']}/100, rubric "
            f"{'-' if rscore is None else rscore}/100 -> combined {combined}/{max_score}."
        ),
        "criteria": criteria,
        "per_question": qa["per_question"],
        "metadata": {
            "judge": "qa-judge",
            "qa_score": qa["score"],
            "rubric_score": rscore,
            "combine": combine,
            "qa_weight": qa_weight,
            "n_questions": len(questions),
        },
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
    base = Path(__file__).resolve().parent
    path = questions_path(base)
    questions = load_questions(path)
    config = json.loads(path.read_text(encoding="utf-8"))
    combine = str(config.get("combine", "sum"))
    qa_weight = float(config.get("qa_weight", 0.5))
    answers, rubric = _collect_judgment(questions, workdir, artifact_url)
    return grade_combined(questions, answers, rubric, combine, qa_weight)


# --------------------------------------------------------------------------
# Answer collection: bounded tool loop (playwright + finish)
# --------------------------------------------------------------------------

def _collect_judgment(
    questions: list[dict[str, Any]], workdir: Path, artifact_url: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
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
            },
            "rubric": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": list(RUBRIC_IDS)},
                        "score": {"type": "integer", "minimum": 1, "maximum": 5},
                        "evidence": {"type": "string"},
                    },
                    "required": ["id", "score", "evidence"],
                },
            },
        },
        "required": ["answers", "rubric"],
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
        {"type": "function", "function": {"name": "finish", "description": "Submit one short answer per question id AND all five rubric ratings.", "parameters": finish_schema}},
    ]

    for call_index in range(1, MAX_MODEL_CALLS + 1):
        tool_choice: str | dict[str, Any] = "auto"
        remaining = getattr(client, "remaining_tokens", None)
        if call_index == MAX_MODEL_CALLS or (remaining is not None and remaining < MIN_REMAINING_TOKENS):
            messages.append({"role": "user", "content": "FINAL CALL: call finish NOW with your best answer for every question id (use \"unknown\" where the page did not communicate it) and all five rubric ratings."})
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
                answers = {row.get("id"): row.get("answer") for row in args.get("answers", [])}
                return answers, args.get("rubric") or []
            if function.get("name") == "playwright":
                output = _run_playwright(args.get("script") or "", workdir, artifact_url)
            else:
                output = f"Unknown tool: {function.get('name')}"
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": output[:12000]})
    return {}, []


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
