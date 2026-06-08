"""Example participant agent (OpenAI tool-loop).

Replace with your own agent — keep these function names and signatures and
`agent.py` keeps working. See agent.md for the full contract.

    info()                              -> dict
    models()                            -> dict   # optional
    generate(workdir)                   -> dict   # writes source/, dist/index.html
    evaluate(workdir, artifact_url)     -> dict   # score + criteria

Three integration patterns:
  - Import an existing Python package and call it inside the hooks.
  - Shell out via subprocess.run to your own CLI.
  - Replace the tool-loop below inline.

Env vars:  OPENAI_API_KEY (local; you set)
           VIS_ARENA_* (cloud; worker-injected, never set yourself)
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from openai import OpenAI


# Local fallback. Cloud jobs override via VIS_ARENA_LLM_MODEL (worker-injected).
# Run `./agent.py models` in a job to print the live list; see README.md for the
# current cloud roster.
LOCAL_DEFAULT_MODEL = "gpt-5.5"

DEFAULT_MODEL = (
    os.environ.get("VIS_ARENA_LLM_MODEL")
    or os.environ.get("VIS_ARENA_OPENAI_MODEL")
    or LOCAL_DEFAULT_MODEL
)


GENERATION_PROMPT = """You are a web data visualization agent.

You are given a WORKDIR containing:
  task.md           the task description — read this first with the bash tool
  data/             task data files

Write the artifact into the SAME WORKDIR:
  source/           editable web source (yours)
  dist/index.html   required, must work without a dev server

Use the bash tool to inspect data and write files. When done, call finish
with a concise JSON summary."""


EVALUATION_PROMPT = """You are an impartial storytelling-visualization evaluator.

Inputs:
  WORKDIR/task.md    read with bash to know what was asked
  ARTIFACT_URL       opens the artifact; use verbatim (do not hardcode localhost)

Open ARTIFACT_URL with playwright (page.goto), then click, hover, resize,
inspect DOM, check console, screenshot. Same procedure for self / peer /
central-judge evaluation.

Score on 100 points across five criteria. Each rated 1-5 on the anchors
below; score = anchor x 4; five criteria x max_score 20 = 100. Required ids:

1. data_fidelity - do displayed values, totals, and trends match WORKDIR/data?
   Read the data with bash, then reconcile against DOM text / screenshot values.
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
criterion. Finish with JSON: score, max_score = 100, summary, criteria (5
items with the ids above, each with score, max_score, anchor, evidence),
browser, artifacts, metadata."""


# ---------------------------------------------------------------------------
# Hooks called by agent.py
# ---------------------------------------------------------------------------

def info() -> dict[str, Any]:
    return {
        "name": "python-openai-template",
        "version": "0.3.0",
        "commands": ["generate", "evaluate"],
        "providers": ["openai", "arena-cloud"],
        "notes": "Simple LLM tool-loop agent with bash and Playwright tools.",
    }


def models() -> dict[str, Any]:
    available = [m.strip() for m in os.environ.get("VIS_ARENA_LLM_MODELS", DEFAULT_MODEL).split(",") if m.strip()]
    return {
        "default_model": DEFAULT_MODEL,
        "available_models": available,
        "select_model": "Set VIS_ARENA_LLM_MODEL to one of available_models.",
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
# Tool loop, LLM client, and tools — replace freely.
# ---------------------------------------------------------------------------

def _run_tool_loop(system_prompt: str, user_prompt: str, tool_root: Path, purpose: str) -> dict[str, Any]:
    client = _make_llm_client(purpose)
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
        message = client.create(model=DEFAULT_MODEL, messages=messages, tools=tools, tool_choice="auto")
        messages.append(message)
        if not message.get("tool_calls"):
            messages.append({"role": "user", "content": "Continue using tools, or call finish with the final JSON."})
            continue
        for call in message["tool_calls"]:
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


class OpenAIChatClient:
    def __init__(self) -> None:
        self.client = OpenAI()

    def create(self, *, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], tool_choice: str) -> dict[str, Any]:
        response = self.client.chat.completions.create(model=model, messages=messages, tools=tools, tool_choice=tool_choice)
        return response.choices[0].message.model_dump(exclude_none=True)


class ArenaChatClient:
    def __init__(self, purpose: str) -> None:
        from vis_arena_sdk import VisArenaClient

        self.job_id = os.environ["VIS_ARENA_JOB_ID"]
        self.purpose = purpose
        self.client = VisArenaClient(
            base_url=os.environ.get("VIS_ARENA_SERVER_URL", "http://host.docker.internal:8000"),
            token=os.environ["VIS_ARENA_API_TOKEN"],
            timeout=180.0,
        )

    def create(self, *, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], tool_choice: str) -> dict[str, Any]:
        response = self.client.create_llm_message(
            job_id=self.job_id,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            model=model,
            purpose=self.purpose,
        )
        return response.message


def _make_llm_client(purpose: str) -> OpenAIChatClient | ArenaChatClient:
    # Cloud: the arena worker injects VIS_ARENA_API_TOKEN + VIS_ARENA_JOB_ID and
    # the agent routes model calls through the arena backend (no provider key
    # needed). Local: you set OPENAI_API_KEY.
    if os.environ.get("VIS_ARENA_API_TOKEN") and os.environ.get("VIS_ARENA_JOB_ID") and not os.environ.get("OPENAI_API_KEY"):
        return ArenaChatClient(purpose)
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit(
            "OPENAI_API_KEY is not set.\n"
            "  Local testing: export OPENAI_API_KEY=sk-... and re-run.\n"
            "  Submitting:   run `vis-arena submit . --dataset ieee-vis-publications`; the arena provides the key."
        )
    return OpenAIChatClient()


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
