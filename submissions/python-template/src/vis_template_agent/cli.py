from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import __version__
from .evaluator import evaluate
from .generator import generate
from .task import load_task


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent", description="Vis Arena template submission agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    info_parser = subparsers.add_parser("info", help="Write agent metadata")
    info_parser.add_argument("--output", required=True)

    generate_parser = subparsers.add_parser("generate", help="Generate a web visualization")
    generate_parser.add_argument("--task", required=True)
    generate_parser.add_argument("--data-dir", required=True)
    generate_parser.add_argument("--output-dir", required=True)

    evaluate_parser = subparsers.add_parser("evaluate", help="Evaluate a generated visualization")
    evaluate_parser.add_argument("--task", required=True)
    evaluate_parser.add_argument("--data-dir", required=True)
    evaluate_parser.add_argument("--source-dir", required=True)
    evaluate_parser.add_argument("--built-dir", required=True)
    evaluate_parser.add_argument("--output", required=True)

    args = parser.parse_args()
    if args.command == "info":
        _write_info(Path(args.output))
    elif args.command == "generate":
        task = load_task(args.task)
        generate(task, Path(args.data_dir), Path(args.output_dir))
    elif args.command == "evaluate":
        task = load_task(args.task)
        evaluate(task, Path(args.data_dir), Path(args.source_dir), Path(args.built_dir), Path(args.output))


def _write_info(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "vis-arena.agent-info.v1",
        "name": "python-template-agent",
        "version": __version__,
        "commands": ["info", "generate", "evaluate"],
        "capabilities": ["static-html-generation", "playwright-evaluation", "source-inspection"],
        "local_llm_environment": ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"],
        "cloud_llm_environment": ["VIS_ARENA_API_TOKEN"]
    }
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

