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

# The judge model is configurable per question set ("model" in questions.json):
# an alias below or a full Bedrock model id. Haiku for cheap continuous use;
# switch to "sonnet" for the final judging run.
MODEL_ALIASES = {
    "haiku": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "sonnet": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "opus": "global.anthropic.claude-opus-4-8",
}


def resolve_model(value: Any) -> str:
    if not value:
        return DEFAULT_MODEL
    return MODEL_ALIASES.get(str(value).strip().lower(), str(value))

# Effectively uncapped: the judge explores as long as it needs; the real stop is the
# per-job token budget. When remaining tokens run low (or at the generous call
# backstop) the loop forces a final answer so a score is ALWAYS produced.
MAX_MODEL_CALLS = 60
# Reserve enough for the final call: by late in a run the conversation carries
# several screenshots, so ONE more exchange can cost far more than a small
# margin — a 60k reserve let runs die mid-call instead of wrapping up.
MIN_REMAINING_TOKENS = 250_000

# The judge scores two aspects from ONE exploration pass:
#   1. QA — does the visualization surface correct answers to grounded questions?
#   2. Rubric — the arena's five public criteria (storytelling quality).
RUBRIC_IDS = ("data_fidelity", "insightfulness", "narrative_coherence", "visual_craft", "functionality")

RUBRIC_ONLY_PROMPT = """You are a careful, impartial visualization judge. You will
be given an interactive data-visualization page. Judge ONLY its quality — there
are no questions to answer in this run.

FIRST take a screenshot and LOOK at the page; explore and interact (click,
filter, hover) and screenshot again after every meaningful interaction. Rate
each criterion 1-5 from what you SEE, not from page text alone:
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

The RENDER STATS accompanying each screenshot (which svg/canvas elements drew)
are context, not a verdict — some legitimate charts are plain HTML/CSS. Use the
screenshot tool as your eyes and the playwright tool (PYTHON,
playwright.sync_api; page URL in VIS_ARENA_ARTIFACT_URL) for interactions —
never page.content(). Do NOT read page source or embedded raw data.

When done, call finish with the five rubric ratings, each with one-line
evidence, and an empty answers list."""


JUDGE_PROMPT = """You are a careful, impartial visualization judge. You will be
given an interactive data-visualization page and a list of factual questions.

You judge TWO aspects in one inspection:

ASPECT 1 — Questions. Find the answers from the interactive visual web page.
FIRST take a screenshot and LOOK at the page; explore and interact (click,
filter, hover) and screenshot again after every meaningful interaction. Read
each answer off the visualizations you can SEE — charts, labelled marks,
tooltips, interaction results. YOU judge whether an answer is visually
supported: a value you can only find as prose, with no visualization
communicating it, is not an answer — reply "unknown" for that question. The
RENDER STATS accompanying each screenshot (which svg/canvas elements actually
drew) are context for that judgment, not a verdict — some legitimate charts
are built from plain HTML/CSS. Do NOT read page source, <script> contents, or
embedded raw data.

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

Rate the rubric from what you SEE in the screenshots, not from page text alone
— e.g. data_fidelity and visual_craft cannot exceed 2 if no chart actually
rendered. Use the screenshot tool as your eyes, and the playwright tool
(PYTHON, playwright.sync_api; page URL in the VIS_ARENA_ARTIFACT_URL env var)
for interactions and targeted text checks — never page.content().

When done, call finish with one short answer per question id (a name, a number,
or a year — no sentences) AND the five rubric ratings with one-line evidence."""


# --------------------------------------------------------------------------
# Question loading + grading (pure functions; unit-tested in the public repo)
# --------------------------------------------------------------------------

def normalize_rows(value: Any) -> list[dict[str, Any]]:
    """Tool arguments arrive shaped differently across models: some return a
    list of objects, some a JSON-encoded string of that list. Iterating a
    string yields characters and crashes the caller, so normalise here and
    drop anything that is not an object rather than raising mid-judgment."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return []
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def load_questions(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    # A rubric-only bundle ships an empty question list on purpose.
    questions = data.get("questions") or []
    for q in questions:
        for field in ("id", "type", "question"):  # cloud bundles are STRIPPED: no answers ride along
            if field not in q:
                raise ValueError(f"question missing field {field!r}")
    return questions


def questions_path(base: Path) -> Path:
    real = base / "questions.json"
    if real.exists():
        return real
    # NEVER silently judge against the dummy fixture: a broken final bundle must fail
    # loudly, not produce plausible-looking scores. Local mechanism testing may opt in.
    if os.environ.get("VIS_ARENA_JOB_ID") or os.environ.get("QA_JUDGE_ALLOW_EXAMPLE") != "1":
        raise SystemExit("questions.json missing from judge bundle — refusing to judge "
                         "(local testing: set QA_JUDGE_ALLOW_EXAMPLE=1 to use the example fixture)")
    return base / "questions.example.json"


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
    # Tolerate short surrounding words ("it is the example dashboard") but never
    # bare substrings of a longer different name.
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
    model = resolve_model(config.get("model"))
    answers, rubric = _collect_judgment(questions, workdir, artifact_url, model)
    # Raw, UNGRADED report: the answer key never enters this container, so grading
    # (QA matching, gates, combination) happens server-side at job completion.
    return {
        "score": None,
        "max_score": 200,
        "summary": "central judge raw report — graded server-side against the private key",
        "answers": [{"id": qid, "answer": answers.get(qid, "")} for qid in (q["id"] for q in questions)],
        "rubric": rubric,
        "render_stats": _last_render_stats,
        "screenshots_taken": _screenshot_count,
        "metadata": {"judge": "qa-judge", "model": model, "n_questions": len(questions)},
    }


# --------------------------------------------------------------------------
# Answer collection: bounded tool loop (playwright + finish)
# --------------------------------------------------------------------------

def _collect_judgment(
    questions: list[dict[str, Any]], workdir: Path, artifact_url: str, model: str = ""
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from llm_client import make_llm_client  # lazy: grading functions stay importable without SDK deps

    client = make_llm_client("evaluation")
    model = model or DEFAULT_MODEL
    # No questions => rubric-only run: a different prompt, so the quality judgment
    # is made the same way whether or not a QA pass exists (keeps validation runs
    # comparable with finals runs).
    if questions:
        question_lines = "\n".join(f"- {q['id']}: {q['question']}" for q in questions)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": JUDGE_PROMPT},
            {"role": "user", "content": f"ARTIFACT_URL={artifact_url}\n\nQuestions:\n{question_lines}"},
        ]
    else:
        messages = [
            {"role": "system", "content": RUBRIC_ONLY_PROMPT},
            {"role": "user", "content": f"ARTIFACT_URL={artifact_url}\n\nJudge this page on the five rubric criteria."},
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
        {
            "type": "function",
            "function": {
                "name": "screenshot",
                "description": "Capture the rendered page and SEE it (returns the image plus mechanical render stats: which svg/canvas elements actually drew). ALWAYS use this before answering, and again after interactions.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "full_page": {"type": "boolean", "description": "capture the full page height (default: viewport only)"},
                        "wait_ms": {"type": "integer", "description": "extra render wait before capture (default 2500)"},
                    },
                },
            },
        },
        {"type": "function", "function": {"name": "finish", "description": "Submit one short answer per question id AND all five rubric ratings.", "parameters": finish_schema}},
    ]

    finish_rejections = 0
    screenshot_failures = 0
    for call_index in range(1, MAX_MODEL_CALLS + 1):
        tool_choice: str | dict[str, Any] = "auto"
        remaining = getattr(client, "remaining_tokens", None)
        if call_index == MAX_MODEL_CALLS or (remaining is not None and remaining < MIN_REMAINING_TOKENS):
            messages.append({"role": "user", "content": "FINAL CALL: call finish NOW with your best answer for every question id (use \"unknown\" where the page did not communicate it) and all five rubric ratings."})
            tool_choice = {"type": "function", "function": {"name": "finish"}}
        message = client.create(model=model, messages=messages, tools=tools, tool_choice=tool_choice)
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
                answers = {str(row.get("id")): row.get("answer") for row in normalize_rows(args.get("answers"))}
                rubric = normalize_rows(args.get("rubric"))
                problems = []
                if _screenshot_count == 0 and screenshot_failures < 2:
                    problems.append("you have not taken a single screenshot — take one and LOOK at the page first")
                missing = [q["id"] for q in questions if q["id"] not in answers]
                if missing:
                    problems.append(f"missing answers for question ids: {', '.join(missing)}")
                rubric_ids = {str(r.get("id")) for r in rubric}
                if rubric_ids != set(RUBRIC_IDS):
                    problems.append(f"rubric must contain exactly these ids: {', '.join(RUBRIC_IDS)}")
                if problems and finish_rejections < 2:
                    finish_rejections += 1
                    messages.append({"role": "tool", "tool_call_id": call["id"],
                                     "content": "finish REJECTED: " + "; ".join(problems) + ". Fix and call finish again."})
                    continue
                return answers, rubric
            if function.get("name") == "screenshot":
                content, error = _take_screenshot(
                    workdir, artifact_url,
                    full_page=bool(args.get("full_page")),
                    wait_ms=int(args.get("wait_ms") or 2500),
                )
                if not content:
                    screenshot_failures += 1
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": content if content else error})
                continue
            if function.get("name") == "playwright":
                output = _run_playwright(args.get("script") or "", workdir, artifact_url)
            else:
                output = f"Unknown tool: {function.get('name')}"
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": output[:12000]})
    return {}, []



_SCREENSHOT_SCRIPT = """
import base64, json, os, sys
from playwright.sync_api import sync_playwright

url, out_path, full_page, wait_ms = sys.argv[1], sys.argv[2], sys.argv[3] == "1", int(sys.argv[4])
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(url, wait_until="load", timeout=60000)
    page.wait_for_timeout(wait_ms)
    stats = page.evaluate(\"\"\"() => {
      const svgs=[...document.querySelectorAll('svg')].map(s=>{const r=s.getBoundingClientRect();return {w:r.width|0,h:r.height|0,children:s.children.length}});
      const canv=[...document.querySelectorAll('canvas')].map(c=>{const r=c.getBoundingClientRect();
        let painted='unknown';
        try{
          const x=c.getContext('2d'); const W=c.width||1, H=c.height||1, S=60;
          const spots=[[0,0],[Math.max(0,(W-S)>>1),Math.max(0,(H-S)>>1)],[Math.max(0,W-S),Math.max(0,H-S)]];
          painted=spots.some(([px,py])=>{const d=x.getImageData(px,py,Math.min(S,W),Math.min(S,H)).data;return Array.from(d).some(v=>v!==0);});
        }catch(e){}
        return {w:r.width|0,h:r.height|0,painted}});
      const ext=[...document.querySelectorAll('script[src],link[href]')].map(e=>e.src||e.href).filter(u=>u&&!u.includes(location.host));
      return {svg:svgs, canvas:canv, external_resources:ext};
    }\"\"\")
    # Bedrock rejects any image whose dimension exceeds 8000px, and a long
    # scrolling artifact easily exceeds that full-page. Clamp instead of failing:
    # a clipped top-of-page screenshot still shows the story; a 502 shows nothing.
    MAX_PX = 7600
    if full_page:
        w, h = page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.scrollHeight]")
        if h > MAX_PX or w > MAX_PX:
            page.screenshot(path=out_path, type="jpeg", quality=70,
                            clip={"x": 0, "y": 0, "width": min(w, MAX_PX), "height": min(h, MAX_PX)})
        else:
            page.screenshot(path=out_path, type="jpeg", quality=70, full_page=True)
    else:
        page.screenshot(path=out_path, type="jpeg", quality=70)
    browser.close()
data = base64.b64encode(open(out_path, "rb").read()).decode()
print(json.dumps({"stats": stats, "b64": data}))
"""

_screenshot_count = 0
_last_render_stats: dict[str, Any] | None = None


def _take_screenshot(workdir: Path, artifact_url: str, full_page: bool = False, wait_ms: int = 2500):
    """Deterministic capture: render stats (what ACTUALLY painted) + a JPEG the model can see.

    The JPEG is also saved into the job workdir so it uploads with the runtime
    files — every judgment leaves a visual audit trail.
    Returns (content_list, None) on success or (None, error_string) on failure.
    """
    global _screenshot_count
    _screenshot_count += 1
    workdir.mkdir(parents=True, exist_ok=True)
    script = workdir / ".qa_judge_screenshot.py"
    script.write_text(_SCREENSHOT_SCRIPT, encoding="utf-8")
    out = workdir / f"judge_screenshot_{_screenshot_count}.jpg"
    try:
        completed = subprocess.run(
            [sys.executable, str(script), artifact_url, str(out), "1" if full_page else "0", str(int(wait_ms))],
            cwd=str(workdir), text=True, errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180,
        )
    except subprocess.TimeoutExpired:
        return None, "screenshot error: page render exceeded 180s"
    if completed.returncode != 0:
        return None, f"screenshot error: {completed.stdout[-1500:]}"
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None, f"screenshot error: unparseable output: {completed.stdout[-500:]}"
    stats = payload["stats"]
    global _last_render_stats
    _last_render_stats = stats
    rendered_svg = sum(1 for x in stats.get("svg", []) if x.get("children", 0) > 0)
    painted_canvas = sum(1 for x in stats.get("canvas", []) if x.get("painted") is True)
    summary = (
        f"RENDER STATS (mechanically measured): svg elements {len(stats.get('svg', []))} "
        f"(with drawn content: {rendered_svg}), canvas elements {len(stats.get('canvas', []))} "
        f"(with painted pixels: {painted_canvas}), external resource refs: "
        f"{stats.get('external_resources', [])}. If nothing is drawn/painted, the page has NO "
        f"working visualizations regardless of its text."
    )
    content = [
        {"type": "text", "text": summary},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{payload['b64']}"}},
    ]
    return content, None


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
