"""Server-side central-judge grading: key stays out of containers."""
from __future__ import annotations

import pytest

from vis_arena_server import judge_grading as jg

KEY = {
    "combine": "sum",
    "questions": [
        {"id": "q1", "type": "text", "question": "top company?", "answer": "Acme Corp", "accept": ["acme"]},
        {"id": "q2", "type": "number", "question": "how many?", "answer": "7", "accept": []},
    ],
}
GATED_KEY = {**KEY, "require_rendered_charts": True}
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


def test_blank_stats_do_not_gate_by_default():
    # Visual-evidence judgment is the judge agent's call; blank probe stats are
    # informational only (e.g. div-based HTML/CSS charts have zero svg/canvas).
    graded = jg.grade_central_result("t", report([{"id": "q1", "answer": "acme corp"}, {"id": "q2", "answer": "7"}], stats=BLANK))
    assert graded["metadata"]["qa_score"] == 100.0
    assert graded["metadata"]["charts_rendered"] is False
    assert graded["score"] == pytest.approx(180.0)
    assert any("visual assessment governs" in n for n in graded["metadata"]["notes"])


def test_gate_applies_only_when_key_opts_in(monkeypatch):
    monkeypatch.setattr(jg, "_load_key", lambda task_id: GATED_KEY)
    graded = jg.grade_central_result("t", report([{"id": "q1", "answer": "acme corp"}, {"id": "q2", "answer": "7"}], stats=BLANK))
    assert graded["metadata"]["qa_score"] == 0.0
    assert graded["score"] == pytest.approx(80.0)  # rubric only survives
    assert "gated" in graded["summary"]


def test_missing_stats_warns_only_when_gate_opted_in(monkeypatch):
    graded = jg.grade_central_result("t", report([{"id": "q1", "answer": "acme"}], stats=None))
    assert graded["metadata"]["qa_score"] == 50.0
    assert not any("could not run" in n for n in graded["metadata"]["notes"])

    monkeypatch.setattr(jg, "_load_key", lambda task_id: GATED_KEY)
    graded = jg.grade_central_result("t", report([{"id": "q1", "answer": "acme"}], stats=None))
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


# ---------------------------------------------------------------------------
# Judge bundle payload: questions only, whitelisted config, no private metadata
# ---------------------------------------------------------------------------

FULL_KEY = {
    "combine": "sum",
    "qa_weight": 0.5,
    "model": "sonnet",
    "require_rendered_charts": False,
    "_dataset": "vast-2024-mc1 mc1.json",
    "_dataset_sha256": "355b6515a5246b8192382dc8b07efe3e6a94f0308aea5af5ff54440fff79d886",
    "_verified": "2026-08-05 margins: q1 +18%, q5 engineered (20 vs <=10)",
    "questions": [
        {"id": "q1", "type": "text", "question": "top company?", "answer": "Acme Corp", "accept": ["acme"]},
        {"id": "q5", "type": "number", "question": "how many?", "answer": "20", "accept": []},
    ],
}


def test_public_questions_payload_strips_answers_and_private_metadata():
    payload = jg.public_questions_payload(FULL_KEY)
    dumped = _json_dumps(payload)
    # no answers, no accept lists, no private underscore metadata (the _verified
    # margin note leaked the q5 answer "20" into judge bundles before this existed)
    assert "acme" not in dumped.lower()
    assert "20 vs" not in dumped
    assert not [k for k in payload if k.startswith("_")]
    assert all(set(q) == {"id", "type", "question"} for q in payload["questions"])
    # scoring config the judge legitimately needs survives
    assert payload["combine"] == "sum"
    assert payload["model"] == "sonnet"
    assert [q["id"] for q in payload["questions"]] == ["q1", "q5"]


def test_public_questions_payload_rejects_unknown_config_keys():
    payload = jg.public_questions_payload({**FULL_KEY, "secret_hint": "the answer is 20"})
    assert "secret_hint" not in payload
    assert "20" not in _json_dumps(payload)


def _json_dumps(value) -> str:
    import json as _j

    return _j.dumps(value)


# ---------------------------------------------------------------------------
# Bundle gate: validate the artifact that ships, independent of how it was built
# ---------------------------------------------------------------------------

def test_bundle_gate_accepts_a_whitelisted_payload():
    assert jg.bundle_violations(jg.public_questions_payload(FULL_KEY), FULL_KEY) == []


def test_bundle_gate_catches_private_metadata_and_answers():
    sloppy = {k: v for k, v in FULL_KEY.items() if k != "questions"}
    sloppy["questions"] = [{"id": q["id"], "type": q["type"], "question": q["question"]} for q in FULL_KEY["questions"]]
    violations = jg.bundle_violations(sloppy, FULL_KEY)
    assert any("_verified" in v for v in violations), violations
    assert any("_dataset_sha256" in v for v in violations), violations

    with_answer = jg.public_questions_payload(FULL_KEY)
    with_answer["questions"][0]["answer"] = "Acme Corp"
    assert any("answer" in v for v in jg.bundle_violations(with_answer, FULL_KEY))


def test_bundle_gate_catches_an_answer_hidden_in_its_own_question_text():
    # authoring hazard the whitelist cannot catch: the question gives itself away
    key = {
        "combine": "sum",
        "questions": [{"id": "q1", "type": "number", "question": "Acme shipped 20 loads — how many?", "answer": "20"}],
    }
    violations = jg.bundle_violations(jg.public_questions_payload(key), key)
    assert any("q1" in v and "own answer" in v for v in violations), violations


def test_bundle_gate_tolerates_incidental_number_matches():
    # "2024" in question text is not a leak of the answer "20"
    key = {
        "combine": "sum",
        "questions": [{"id": "q1", "type": "number", "question": "In 2024, how many loads?", "answer": "20"}],
    }
    assert jg.bundle_violations(jg.public_questions_payload(key), key) == []


def test_bundle_gate_does_not_flag_choice_questions_listing_their_answer():
    # A multiple-choice question MUST contain its answer among the options —
    # flagging that would make the gate cry wolf until someone switches it off.
    key = {
        "combine": "sum",
        "questions": [{
            "id": "q5", "type": "choice", "answer": "Harrell-Walters",
            "question": "Which of Harrell-Walters, Wilcox-Nelson, or Clarke and Sloan did the most?",
        }],
    }
    assert jg.bundle_violations(jg.public_questions_payload(key), key) == []


def test_bundle_gate_still_flags_non_choice_self_disclosure():
    key = {
        "combine": "sum",
        "questions": [{"id": "q1", "type": "text", "answer": "Harrell-Walters",
                       "question": "Harrell-Walters led the year — who led it?"}],
    }
    assert jg.bundle_violations(jg.public_questions_payload(key), key)


# ---------------------------------------------------------------------------
# Module modes: rubric-only (validation on public tasks), qa-only, both
# ---------------------------------------------------------------------------

RUBRIC_ONLY_KEY = {"mode": "rubric", "model": "global.anthropic.claude-sonnet-5"}


def test_rubric_only_mode_scores_out_of_100_without_questions(monkeypatch):
    monkeypatch.setattr(jg, "_load_key", lambda task_id: RUBRIC_ONLY_KEY)
    graded = jg.grade_central_result("public-task", {"rubric": RUBRIC, "screenshots_taken": 2})
    assert graded["score"] == pytest.approx(80.0)
    assert graded["max_score"] == 100
    assert graded["metadata"]["qa_score"] is None
    assert graded["metadata"]["rubric_score"] == pytest.approx(80.0)
    assert graded["per_question"] == []
    assert len(graded["criteria"]) == 5


def test_rubric_only_mode_ignores_any_answers_the_judge_returns(monkeypatch):
    monkeypatch.setattr(jg, "_load_key", lambda task_id: RUBRIC_ONLY_KEY)
    graded = jg.grade_central_result(
        "public-task", {"answers": [{"id": "q1", "answer": "whatever"}], "rubric": RUBRIC}
    )
    assert graded["max_score"] == 100
    assert graded["metadata"]["qa_score"] is None


def test_rubric_only_mode_fails_loudly_when_rubric_missing(monkeypatch):
    monkeypatch.setattr(jg, "_load_key", lambda task_id: RUBRIC_ONLY_KEY)
    graded = jg.grade_central_result("public-task", {"rubric": RUBRIC[:2]})
    assert graded["score"] is None
    assert "rubric" in graded["summary"].lower()


def test_both_mode_remains_the_default(monkeypatch):
    graded = jg.grade_central_result("t", report([{"id": "q1", "answer": "acme corp"}, {"id": "q2", "answer": "7"}]))
    assert graded["max_score"] == 200
