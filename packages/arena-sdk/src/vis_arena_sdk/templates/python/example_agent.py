"""Example Vis Arena participant agent (OpenAI tool-loop).

This is an EXAMPLE implementation. Replace it with your own agent — keep these
function names and signatures and `agent.py` will keep working:

    info()                              -> dict
    models()                            -> dict   # optional
    generate(workdir)                   -> dict   # writes source/, dist/index.html
    evaluate(workdir, artifact_url)     -> dict   # score + criteria

Three integration patterns if you already have an agent:

  (1) Import your existing Python package
      from my_agent import build as my_build
      def generate(workdir):
          my_build(task=workdir / "task.md", data=workdir / "data",
                   out=workdir / "dist")
          return {"notes": "wrapped my_agent.build"}

  (2) Shell out to your CLI
      def generate(workdir):
          subprocess.run([sys.executable, "-m", "my_agent",
                          "--task", str(workdir / "task.md"),
                          "--data", str(workdir / "data"),
                          "--out", str(workdir / "dist")], check=True)
          return {"notes": "wrapped my_agent CLI"}

  (3) Inline implementation — replace the OpenAI tool-loop below with your own.

Environment variables you may care about:

  Local laptop testing:
    OPENAI_API_KEY            you set this; the only var local testing needs.

  Cloud evaluation (injected automatically by the arena worker — never set these yourself):
    VIS_ARENA_API_TOKEN       short-lived token to call the arena backend.
    VIS_ARENA_SERVER_URL      arena backend URL.
    VIS_ARENA_JOB_ID          current job id.
    VIS_ARENA_LLM_MODEL       chosen model id.
    VIS_ARENA_LLM_MODELS      comma-separated list of available cloud models.
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


# Local fallback when no cloud override is present. Cloud jobs replace this via
# VIS_ARENA_LLM_MODEL. Change this string to use a different model locally.
LOCAL_DEFAULT_MODEL = "gpt-4.1-mini"

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


EVALUATION_PROMPT = """You are an impartial web visualization evaluator.

You are given exactly two things:
  WORKDIR/task.md      read this first with the bash tool to know what was asked
  ARTIFACT_URL         opens the artifact in the browser; use the URL verbatim
                       (do not reconstruct it; do not hardcode localhost:8080)

The artifact is your only direct evidence of the work. Open ARTIFACT_URL with
the playwright tool (page.goto(ARTIFACT_URL)) and interact with the live
page — click, hover, resize the viewport, inspect the DOM, check the console,
capture screenshots. This is the same procedure whether you are judging your
own artifact, a peer's, or the central judger's slot.

Score on a 100-point scale: max_score MUST be 100, and the per-criterion
`score` values should sum to the overall `score`. Pick criteria sensibly for
the task (typical: task_fit, data_accuracy, interactivity, design_clarity,
responsiveness_robustness). Allocate per-criterion `max_score` weights so they
sum to 100.

When done, call finish with JSON containing score, max_score, summary,
criteria, browser, artifacts, and metadata."""


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
