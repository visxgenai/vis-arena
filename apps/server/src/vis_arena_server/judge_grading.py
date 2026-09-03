"""Server-side grading for central-judge evaluations.

The judge container never holds the answer key: its bundle carries QUESTIONS
ONLY, and its report returns raw answers + rubric + mechanical render stats.
Grading happens here, against a key loaded from private S3 — a prompt-injected
judge can emit garbage, but it cannot read or leak answers it never had.

Key files live at  s3://<bucket>/<prefix>/<task_id>.json  with the same schema
as the private questions.json (questions incl. answer/accept + config).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

from .settings import settings

RUBRIC_IDS = ("data_fidelity", "insightfulness", "narrative_coherence", "visual_craft", "functionality")


# ---------------------------------------------------------------------------
# Answer matching (mirrors the public harness mechanism; content stays private)
# ---------------------------------------------------------------------------

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
    accepted = {normalize_answer(question["answer"])} | {normalize_answer(v) for v in question.get("accept", [])}
    if question.get("type") in ("number", "year"):
        expected = _numbers_in(str(question["answer"]))
        return bool(expected) and _numbers_in(normalized) == expected
    if normalized in accepted:
        return True
    return any(acc and re.search(rf"(^|\s){re.escape(acc)}($|\s)", normalized) for acc in accepted)


def rubric_score(rubric: list[dict[str, Any]] | None) -> float | None:
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
    # Span the full 0-100 range: five 1s (nothing works) -> 0, five 5s -> 100.
    # A plain sum x 4 floors at 20, which compressed the bottom of the scale and
    # made the judge look lenient on broken artifacts that peers scored near 0.
    return round((total - len(RUBRIC_IDS)) / (4 * len(RUBRIC_IDS)) * 100, 1)


def _charts_rendered(render_stats: dict[str, Any] | None) -> bool | None:
    """True/False when stats are present and conclusive; None when absent."""
    if not isinstance(render_stats, dict):
        return None
    svgs = render_stats.get("svg") or []
    canvases = render_stats.get("canvas") or []
    drawn = sum(1 for s in svgs if isinstance(s, dict) and (s.get("children") or 0) > 0)
    painted = sum(1 for c in canvases if isinstance(c, dict) and c.get("painted") is True)
    return (drawn + painted) > 0


# Config the judge container legitimately needs; everything else in a key file
# (answers, accept lists, dataset hashes, verification notes) stays server-side.
# Whitelist, never blacklist: the "_verified" margin note used to reach bundles
# and it spelled out an engineered answer value.
PUBLIC_KEY_FIELDS = ("combine", "qa_weight", "model", "require_rendered_charts")
PUBLIC_QUESTION_FIELDS = ("id", "type", "question")


def public_questions_payload(key: dict[str, Any]) -> dict[str, Any]:
    """The questions.json that ships inside a judge bundle: questions without
    answers, plus whitelisted scoring config. Build judge bundles from this —
    never from the raw key file."""
    payload = {field: key[field] for field in PUBLIC_KEY_FIELDS if field in key}
    payload["questions"] = [
        {field: question[field] for field in PUBLIC_QUESTION_FIELDS if field in question}
        for question in key.get("questions", [])
    ]
    return payload


def bundle_violations(payload: dict[str, Any], key: dict[str, Any]) -> list[str]:
    """Validate the questions.json that is ABOUT TO SHIP against the private key.

    Checks the artifact, not the code that produced it, so a future script that
    forgets `public_questions_payload` is still caught. Three classes:
      1. unexpected top-level or per-question fields (private metadata riding along),
      2. any answer/accept value present anywhere in the payload,
      3. a question whose own text contains its answer (authoring hazard the
         whitelist cannot catch).
    Returns human-readable violations; empty list means safe to ship.
    """
    violations: list[str] = []

    for field in payload:
        if field not in (*PUBLIC_KEY_FIELDS, "questions"):
            violations.append(f"unexpected top-level field in bundle: {field!r}")
    for question in payload.get("questions", []):
        extra = set(question) - set(PUBLIC_QUESTION_FIELDS)
        if extra:
            violations.append(f"unexpected field(s) on question {question.get('id')!r}: {sorted(extra)}")

    # A multiple-choice question necessarily lists its answer among the options,
    # so answer-presence checks apply only to free-answer types.
    free_answer = [q for q in key.get("questions", []) if q.get("type") != "choice"]

    blob = json.dumps(payload).lower()
    for question in free_answer:
        for secret in (question.get("answer"), *question.get("accept", [])):
            text = str(secret or "").strip().lower()
            # Only flag distinctive values; a bare number is checked per-question below.
            if len(text) >= 4 and not text.isdigit() and text in blob:
                violations.append(f"answer value for {question.get('id')!r} appears in the bundle")

    for question in free_answer:
        answer = str(question.get("answer") or "").strip().lower()
        shipped = next((q for q in payload.get("questions", []) if q.get("id") == question.get("id")), None)
        if not answer or shipped is None:
            continue
        question_text = str(shipped.get("question") or "").lower()
        pattern = rf"(?<![\w.]){re.escape(answer)}(?![\w.])"
        if re.search(pattern, question_text):
            violations.append(f"question {question.get('id')!r} contains its own answer")

    return violations


@lru_cache(maxsize=32)
def _load_key(task_id: str) -> dict[str, Any]:
    from .storage import read_s3_file  # late import: storage pulls boto3/settings

    key_path = f"{settings.central_judge_keys_s3_prefix.rstrip('/')}/{task_id}.json"
    body, _ = read_s3_file(key_path)
    data = json.loads(body.decode("utf-8"))
    if not data.get("questions") and str(data.get("mode", "both")) != "rubric":
        raise ValueError(f"answer key for {task_id} has no questions")
    return data


def grade_central_result(task_id: str, raw_report: dict[str, Any]) -> dict[str, Any]:
    """Grade a judge container's raw report against the private key.

    Never raises on judge-output problems (missing answers/rubric degrade the
    score, not the job); raises only if the key itself is unavailable/broken.
    """
    key = _load_key(task_id)
    # Modules can run independently: "rubric" scores presentation only (usable on
    # public tasks with no answer key, e.g. validating the judge against peer
    # scores), "qa" scores answers only, "both" is the finals default.
    mode = str(key.get("mode", "both")).lower()
    questions = key.get("questions") or []
    combine = str(key.get("combine", "sum"))
    qa_weight = float(key.get("qa_weight", 0.5))
    # Visual-evidence judgment belongs to the (vision-equipped) judge agent, not a
    # deterministic pixel rule — e.g. a legitimate div-based HTML/CSS chart has zero
    # svg/canvas and would be zeroed unfairly. The mechanical gate stays available as
    # an explicit opt-in only.
    require_rendered = bool(key.get("require_rendered_charts", False))

    scores_qa = mode in ("both", "qa")
    per_question: list[dict[str, Any]] = []
    qa: float | None = None
    if scores_qa:
        answers_list = raw_report.get("answers") or []
        answers = {str(a.get("id")): a.get("answer") for a in answers_list if isinstance(a, dict)}
        per_question = [{"id": q["id"], "correct": is_correct(q, answers.get(q["id"]))} for q in questions]
        correct = sum(1 for r in per_question if r["correct"])
        qa = round(100.0 * correct / len(questions), 1) if questions else 0.0

    notes: list[str] = []
    charts = _charts_rendered(raw_report.get("render_stats"))
    if require_rendered and charts is False and scores_qa:
        qa = 0.0
        notes.append("QA gated to 0: no rendered visualizations (0 drawn svg / 0 painted canvas) — answers were prose-only by definition")
    if charts is False:
        notes.append("info: probe saw no drawn svg/painted canvas — judge's visual assessment governs")
    if require_rendered and charts is None:
        notes.append("warning: no render stats in judge report — visual gate could not run")
    if not raw_report.get("screenshots_taken"):
        notes.append("warning: judge produced no screenshots")

    rubric = raw_report.get("rubric") or []
    rscore = rubric_score(rubric)
    max_score = 100
    if mode == "rubric":
        combined = rscore
        blend = "rubric only"
        if rscore is None:
            notes.append("rubric incomplete or invalid — no score produced")
    elif not scores_qa:
        combined = rscore
        blend = "rubric only"
    elif mode == "qa":
        combined = qa
        blend = "QA only"
    elif rscore is None:
        combined = qa
        blend = "QA only — rubric incomplete"
        notes.append("rubric incomplete — combined score is QA-only (/100)")
    elif combine == "weighted":
        combined = round(qa_weight * qa + (1.0 - qa_weight) * rscore, 1)
        blend = f"{round(qa_weight * 100)}% QA + {round((1.0 - qa_weight) * 100)}% rubric"
    else:
        combined = round(qa + rscore, 1)
        max_score = 200
        blend = "QA + rubric, direct sum"

    criteria = []
    if rscore is not None:
        by_id = {str(r.get("id")): r for r in rubric}
        criteria = [
            {"id": rid, "score": max(1, min(5, int(by_id[rid].get("score")))), "max_score": 5,
             "evidence": str(by_id[rid].get("evidence") or "")[:300]}
            for rid in RUBRIC_IDS
        ]
    return {
        "score": combined,
        "max_score": max_score,
        "summary": (
            f"Central judge ({blend}): QA {qa}/100, rubric {'-' if rscore is None else rscore}/100 "
            f"-> combined {combined}/{max_score}." + ("".join(f" [{n}]" for n in notes) if notes else "")
        ),
        "criteria": criteria,
        "per_question": per_question,
        "metadata": {
            "judge": "qa-judge",
            "graded": "server-side",
            "qa_score": qa,
            "mode": mode,
            "rubric_score": rscore,
            "combine": combine,
            "charts_rendered": charts,
            "screenshots_taken": raw_report.get("screenshots_taken"),
            "n_questions": len(questions),
            "notes": notes,
        },
    }
