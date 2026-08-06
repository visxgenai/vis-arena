"""Server-side central-judge grading: key stays out of containers."""
from __future__ import annotations

import pytest

from vis_arena_server import judge_grading as jg

KEY = {
    "combine": "sum",
    "require_rendered_charts": True,
    "questions": [
        {"id": "q1", "type": "text", "question": "top company?", "answer": "Acme Corp", "accept": ["acme"]},
        {"id": "q2", "type": "number", "question": "how many?", "answer": "7", "accept": []},
    ],
}
RUBRIC = [
    {"id": "data_fidelity", "score": 4, "evidence": "x"},
    {"id": "insightfulness", "score": 3, "evidence": "x"},
    {"id": "narrative_coherence", "score": 4, "evidence": "x"},
    {"id": "visual_craft", "score": 5, "evidence": "x"},
    {"id": "functionality", "score": 4, "evidence": "x"},
]
RENDERED = {"svg": [{"w": 500, "h": 300, "children": 12}], "canvas": []}
BLANK = {"svg": [{"w": 500, "h": 300, "children": 0}], "canvas": [{"w": 300, "h": 150, "painted": False}]}


@pytest.fixture(autouse=True)
def _mock_key(monkeypatch):
    jg._load_key.cache_clear()
    monkeypatch.setattr(jg, "_load_key", lambda task_id: KEY)


def report(answers, rubric=RUBRIC, stats=RENDERED, shots=2):
    return {"answers": answers, "rubric": rubric, "render_stats": stats, "screenshots_taken": shots}


def test_full_marks_with_rendered_charts():
    graded = jg.grade_central_result("t", report([{"id": "q1", "answer": "acme corp"}, {"id": "q2", "answer": "7"}]))
    assert graded["metadata"]["qa_score"] == 100.0
    assert graded["score"] == pytest.approx(180.0)  # 100 + 80
    assert graded["max_score"] == 200


def test_qa_gated_to_zero_without_rendered_charts():
    graded = jg.grade_central_result("t", report([{"id": "q1", "answer": "acme corp"}, {"id": "q2", "answer": "7"}], stats=BLANK))
    assert graded["metadata"]["qa_score"] == 0.0
    assert graded["metadata"]["charts_rendered"] is False
    assert graded["score"] == pytest.approx(80.0)  # rubric only survives
    assert "gated" in graded["summary"]


def test_missing_stats_warns_but_does_not_gate():
    graded = jg.grade_central_result("t", report([{"id": "q1", "answer": "acme"}], stats=None))
    assert graded["metadata"]["qa_score"] == 50.0
    assert any("could not run" in n for n in graded["metadata"]["notes"])


def test_missing_answers_and_partial_rubric_degrade_safely():
    graded = jg.grade_central_result("t", report([], rubric=RUBRIC[:3]))
    assert graded["metadata"]["qa_score"] == 0.0
    assert graded["metadata"]["rubric_score"] is None
    assert graded["max_score"] == 100  # QA-only fallback, loudly noted
    assert any("rubric incomplete" in n for n in graded["metadata"]["notes"])


def test_no_screenshots_noted():
    graded = jg.grade_central_result("t", report([{"id": "q1", "answer": "acme"}], shots=0))
    assert any("no screenshots" in n for n in graded["metadata"]["notes"])


def test_report_never_leaks_answers():
    import json as _json
    graded = jg.grade_central_result("t", report([{"id": "q1", "answer": "totally wrong"}]))
    dumped = _json.dumps(graded).lower()
    assert "acme corp" not in dumped.replace("totally wrong", "")  # key answer absent
    assert "top company" not in dumped                              # question text absent
