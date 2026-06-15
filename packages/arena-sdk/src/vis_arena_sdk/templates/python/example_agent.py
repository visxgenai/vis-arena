"""Example participant agent (LLM ReAct loop).

Replace with your own agent — keep these function names and signatures and
`agent.py` keeps working. See agent.md for the full contract.

    info()                              -> dict   # required: describe your agent
    models()                            -> dict   # optional: list models you can use
    generate(workdir)                   -> dict   # required: build the artifact
    evaluate(workdir, artifact_url)     -> dict   # required: score it

Three integration patterns:
  - Import an existing Python package and call it inside the hooks.
  - Shell out via subprocess.run to your own CLI.
  - Replace the tool-loop below inline.

Env vars:  OPENAI_API_KEY (local; you set)
           VIS_ARENA_* (cloud; worker-injected, never set yourself)

LLM call routing (local OpenAI vs arena cloud broker) lives in llm_client.py.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from llm_client import make_llm_client


# Choose your model in code: pass model=... per LLM call (see client.create below), or edit these.
# Arena cloud allow-list (snapshot; `./agent.py models` prints the live list):
#   haiku-4-5 (cheapest), sonnet-4-5 (balanced), opus-4-8/4-7 (priciest).
# Call wrappers live in llm_client.py.
CLOUD_MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
LOCAL_MODEL = "gpt-5.5"
# Cloud jobs set VIS_ARENA_JOB_ID; local runs don't.
DEFAULT_MODEL = CLOUD_MODEL if os.environ.get("VIS_ARENA_JOB_ID") else LOCAL_MODEL


GENERATION_PROMPT = """You are a web data visualization agent.

Use a brief ReAct workflow:
1. Inspect WORKDIR/task.md and list WORKDIR/data.
2. Read only small schema/header samples, not the full dataset.
3. Write a polished interactive artifact to WORKDIR/dist/index.html.
4. Optionally verify the file exists, then call finish with a concise JSON
   summary.

The artifact must work without a dev server. If it needs runtime data, copy or
embed only what is necessary inside dist/ so previewing dist/index.html works."""


EVALUATION_PROMPT = """You are an impartial storytelling-visualization evaluator.

The leaderboard aggregates raw scores, so the overall "score" MUST be on a
0-100 scale (max_score = 100). Do not use a 0-10 or other scale.

Inputs:
  WORKDIR/task.md    read with bash to know what was asked
  ARTIFACT_URL       opens the artifact; use verbatim (do not hardcode localhost)

Use a brief ReAct workflow:
1. Open ARTIFACT_URL with Playwright.
2. Inspect visible DOM text, console errors, layout, and core interactions.
3. Call finish with the score JSON.

Do not load the raw dataset during evaluation.

Rate each of the five criteria 1-5 using the anchors below. For each criterion
set "score" to that 1-5 level and "max_score" to 5. The overall "score" is the
sum of the five levels x 4 (so 0-100), with overall "max_score" = 100. Required ids:

1. data_fidelity - do displayed values, totals, and trends look internally
   consistent and match what the task asks for?
     1 fabricated or contradicts data · 2 major mismatch in key values ·
     3 mostly faithful, minor discrepancies · 4 faithful, all spot-checks pass ·
     5 fully faithful incl. aggregations, units, and edge cases.

2. insightfulness - does the artifact go beyond plotting to identify trends,
   exceptions, and implications?
     1 raw chart · 2 basic observation · 3 headline pattern called out ·
     4 trends + exceptions + comparison · 5 rich, actionable, decision-pointing.

3. narrative_coherence - story arc (hook -> build -> payoff) AND internal
   consistency across panels (no contradictions; encodings hold)?
     1 contradictory or no story · 2 disconnected panels, no setup or takeaway ·
     3 mostly coherent, implicit takeaway · 4 clear sections, consistent
     encodings, reasoned transitions · 5 tight arc; every panel reinforces a
     distinct hook and decisive payoff.

4. visual_craft - chart type + encodings + axes + labels/captions + legibility,
   including disclosure of filters / time frames / aggregations / scope?
     1 misrepresents or illegible · 2 suboptimal type or major label gaps ·
     3 readable, basic labels cover main filters · 4 well-matched type +
     encodings + captions name assumptions · 5 optimal, accessible color,
     comprehensive disclosure (filters + time + scope + exclusions).

5. functionality - do interactive controls (filters, tooltips, selection,
   resize) work? SCORE ONLY FROM INTERACTIONS YOU ACTUALLY PERFORMED, not
   from reading source code.
     1 broken / console errors · 2 some controls dead · 3 core interactions
     work · 4 all controls work as implied · 5 all work and meaningfully aid
     analysis.

Attach short evidence strings (DOM, screenshot, console, interaction) per
criterion. Finish with JSON: score (0-100), max_score = 100, summary, criteria
(5 items with the ids above, each with score 1-5, max_score = 5, anchor,
evidence), browser, artifacts, metadata."""


# ---------------------------------------------------------------------------
# Hooks called by agent.py
# ---------------------------------------------------------------------------

def info() -> dict[str, Any]:
    """Required. Describe your agent (name, version). The arena records it each run."""
    return {
        "name": "python-openai-template",
        "version": "0.3.0",
        "commands": ["generate", "evaluate"],
        "providers": ["openai", "arena-cloud"],
        "notes": "Small ReAct agent with bash and Playwright tools.",
    }


def models() -> dict[str, Any]:
    """Optional. Lists the cloud models you can use — run `./agent.py models`."""
    available = [m.strip() for m in os.environ.get("VIS_ARENA_LLM_MODELS", DEFAULT_MODEL).split(",") if m.strip()]
    return {
        "default_model": DEFAULT_MODEL,
        "available_models": available,
        "select_model": "Pass model=<one of available_models> on each LLM call; the arena sets the default + allow-list.",
    }


def generate(workdir: Path) -> dict[str, Any]:
    return _run_tool_loop(
        GENERATION_PROMPT,
        f"WORKDIR={workdir}",
        tool_root=workdir,
        purpose="generation",
    )


def evaluate(workdir: Path, artifact_url: str) -> dict[str, Any]:
    return _run_tool_loop(
        EVALUATION_PROMPT,
        f"WORKDIR={workdir}\nARTIFACT_URL={artifact_url}",
        tool_root=workdir,
        purpose="evaluation",
    )


# ---------------------------------------------------------------------------
# Tool loop and tools — replace freely. (LLM call wrappers are in llm_client.py.)
# ---------------------------------------------------------------------------

def _run_tool_loop(system_prompt: str, user_prompt: str, tool_root: Path, purpose: str) -> dict[str, Any]:
    print(f"[agent] {purpose}: model={DEFAULT_MODEL}", file=sys.stderr)
    client = make_llm_client(purpose)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": "Run a shell command. Use this to inspect data and create or read files.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "cwd": {"type": "string", "description": "Optional working directory."},
                    },
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "playwright",
                "description": "Run a Python Playwright script generated by the agent. The script can open ARTIFACT_URL, inspect the DOM, take screenshots, check console logs, and exercise interactions.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "script": {"type": "string"},
                        "cwd": {"type": "string", "description": "Optional working directory."},
                    },
                    "required": ["script"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finish",
                "description": "Return the final JSON result.",
                "parameters": {"type": "object", "properties": {"result": {"type": "object"}}, "required": ["result"]},
            },
        },
    ]

    while True:
        # model=... is per call — pass any allowed model here (e.g. a cheaper model for
        # planning steps and a pricier one for the final render). DEFAULT_MODEL is just a default.
        message = client.create(model=DEFAULT_MODEL, messages=messages, tools=tools, tool_choice="auto")
        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            messages.append({"role": "user", "content": "Continue with the brief workflow using tools, or call finish with the final JSON."})
            continue
        for call in calls:
            function = call.get("function") or {}
            try:
                args = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError as exc:
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": f"Tool argument JSON error: {exc}"})
                continue
            if function.get("name") == "finish":
                return args.get("result") or {}
            if function.get("name") == "bash":
                command = args.get("command")
                output = _run_bash(command, Path(args.get("cwd") or tool_root)) if command else "Tool error: bash requires a command string."
            elif function.get("name") == "playwright":
                script = args.get("script")
                output = _run_playwright(script, Path(args.get("cwd") or tool_root)) if script else "Tool error: playwright requires a script string."
            else:
                output = f"Unknown tool: {function.get('name')}"
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": output[:12000]})
        if purpose == "generation" and (tool_root / "dist" / "index.html").exists():
            messages.append({"role": "user", "content": "dist/index.html exists. Verify only if needed, then call finish."})


def _run_bash(command: str, cwd: Path) -> str:
    cwd.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
    )
    return f"$ {command}\nexit={completed.returncode}\n{completed.stdout}"


def _run_playwright(script: str, cwd: Path) -> str:
    wrapper = cwd / ".vis_arena_playwright.py"
    wrapper.write_text(script, encoding="utf-8")
    return _run_bash(f"{shlex.quote(sys.executable)} {shlex.quote(str(wrapper))}", cwd)
