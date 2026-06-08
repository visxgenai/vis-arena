#!/usr/bin/env python3
"""Vis Arena template agent.

Environment variables — you do NOT create a .env file:

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

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import UTC, datetime
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
Build a complete browser-ready visualization for the task.
Use the bash tool to inspect data and write files. Put editable source in SOURCE_DIR
and compiled/static browser artifacts in DIST_DIR. The dist artifact must include
index.html and work without a dev server. When done, call finish with a concise JSON
summary."""


EVALUATION_PROMPT = """You are an impartial web visualization evaluator.
Evaluate the submission against the task rubric. Prefer browser evidence by using
the playwright tool. Use source inspection only for behavior that is hard to observe
interactively, such as animation timing or hidden data transforms. When done, call
finish with JSON containing score, max_score, summary, criteria, browser,
source_observations, artifacts, and metadata."""


def main() -> None:
    parser = argparse.ArgumentParser(description="Vis Arena template LLM agent")
    sub = parser.add_subparsers(dest="command", required=True)

    info = sub.add_parser("info")
    info.add_argument("--output", required=True)

    models = sub.add_parser("models")
    models.add_argument("--output")

    gen = sub.add_parser("generate")
    gen.add_argument("--task", required=True)
    gen.add_argument("--data-dir", required=True)
    gen.add_argument("--output-dir", required=True)

    ev = sub.add_parser("evaluate")
    ev.add_argument("--task", required=True)
    ev.add_argument("--data-dir", required=True)
    ev.add_argument("--source-dir", required=True)
    ev.add_argument("--dist-dir", required=True)
    ev.add_argument("--output", required=True)

    args = parser.parse_args()
    if args.command == "info":
        write_json(Path(args.output), info_payload())
    elif args.command == "models":
        payload = models_payload()
        if args.output:
            write_json(Path(args.output), payload)
        else:
            print(json.dumps(payload, indent=2))
    elif args.command == "generate":
        run_generate(args)
    elif args.command == "evaluate":
        run_evaluate(args)


def info_payload() -> dict[str, Any]:
    return {
        "schema_version": "vis-arena.agent.v1",
        "name": "python-openai-template",
        "version": "0.2.0",
        "commands": ["generate", "evaluate"],
        "providers": ["openai", "arena-cloud"],
        "notes": "Simple LLM tool-loop agent with bash and Playwright tools.",
    }


def models_payload() -> dict[str, Any]:
    models = [model.strip() for model in os.environ.get("VIS_ARENA_LLM_MODELS", DEFAULT_MODEL).split(",") if model.strip()]
    return {
        "default_model": DEFAULT_MODEL,
        "available_models": models,
        "select_model": "Set VIS_ARENA_LLM_MODEL to one of available_models.",
    }


def run_generate(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir).resolve()
    source_dir = output_dir / "source"
    dist_dir = output_dir / "dist"
    source_dir.mkdir(parents=True, exist_ok=True)
    dist_dir.mkdir(parents=True, exist_ok=True)

    task_text = Path(args.task).read_text(encoding="utf-8")
    data_dir = Path(args.data_dir).resolve()
    prompt = f"""
TASK_FILE={Path(args.task).resolve()}
DATA_DIR={data_dir}
SOURCE_DIR={source_dir}
DIST_DIR={dist_dir}

Task:
{task_text}
"""
    result = run_agent(GENERATION_PROMPT, prompt, tool_root=output_dir, purpose="generation")
    if not (dist_dir / "index.html").exists():
        raise SystemExit("Generation failed: dist/index.html was not created")
    write_json(
        output_dir / "generation.json",
        {
            "schema_version": "vis-arena.generation.v1",
            "task_id": extract_task_id(task_text),
            "entrypoint": "index.html",
            "source_dir": "source",
            "dist_dir": "dist",
            "created_at": now(),
            "notes": result.get("summary", "Generated by template LLM agent."),
        },
    )


def run_evaluate(args: argparse.Namespace) -> None:
    output = Path(args.output).resolve()
    report_dir = output.parent
    report_dir.mkdir(parents=True, exist_ok=True)

    task_text = Path(args.task).read_text(encoding="utf-8")
    prompt = f"""
TASK_FILE={Path(args.task).resolve()}
DATA_DIR={Path(args.data_dir).resolve()}
SOURCE_DIR={Path(args.source_dir).resolve()}
DIST_DIR={Path(args.dist_dir).resolve()}
REPORT_DIR={report_dir}
ENTRYPOINT={(Path(args.dist_dir).resolve() / "index.html").as_uri()}

Task and rubric:
{task_text}
"""
    result = run_agent(EVALUATION_PROMPT, prompt, tool_root=report_dir, purpose="evaluation")
    report = normalize_evaluation(result, extract_task_id(task_text))
    write_json(output, report)


def run_agent(system_prompt: str, user_prompt: str, tool_root: Path, purpose: str) -> dict[str, Any]:
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
                "description": "Run a Python Playwright script generated by the agent. The script can inspect pages, screenshots, DOM, console logs, and interactions.",
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
                output = run_bash(command, Path(args.get("cwd") or tool_root)) if command else "Tool error: bash requires a command string."
            elif function.get("name") == "playwright":
                script = args.get("script")
                output = run_playwright(script, Path(args.get("cwd") or tool_root)) if script else "Tool error: playwright requires a script string."
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


def make_llm_client(purpose: str) -> OpenAIChatClient | ArenaChatClient:
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


def run_bash(command: str, cwd: Path) -> str:
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


def run_playwright(script: str, cwd: Path) -> str:
    wrapper = cwd / ".vis_arena_playwright.py"
    wrapper.write_text(script, encoding="utf-8")
    return run_bash(f"{shlex.quote(sys.executable)} {shlex.quote(str(wrapper))}", cwd)


def normalize_evaluation(result: dict[str, Any], task_id: str) -> dict[str, Any]:
    score = float(result.get("score", 0))
    max_score = float(result.get("max_score", 100))
    return {
        "schema_version": "vis-arena.evaluation.v1",
        "task_id": task_id,
        "score": max(0.0, min(score, max_score)),
        "max_score": max_score,
        "summary": str(result.get("summary", "")),
        "criteria": result.get("criteria", []),
        "browser": result.get("browser", {}),
        "source_observations": result.get("source_observations", []),
        "artifacts": result.get("artifacts", {}),
        "metadata": result.get("metadata", {}) | {"evaluated_at": now(), "evaluator": "python-openai-template"},
    }


def extract_task_id(task_text: str) -> str:
    if task_text.startswith("---"):
        header = task_text.split("---", 2)[1]
        for line in header.splitlines():
            if line.startswith("id:"):
                return line.split(":", 1)[1].strip()
    return "unknown-task"


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def now() -> str:
    return datetime.now(UTC).isoformat()


if __name__ == "__main__":
    main()
