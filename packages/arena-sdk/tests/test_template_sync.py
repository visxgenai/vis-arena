"""The template vendored into the SDK must match submissions/python-template.

The vendored copy is what `vis-arena init` scaffolds. This test prevents the
two copies from silently drifting.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
VENDORED = Path(__file__).resolve().parents[1] / "src" / "vis_arena_sdk" / "templates" / "python"
SOURCE = REPO_ROOT / "submissions" / "python-template"

SHARED_FILES = ["agent.py", "example_agent.py", "llm_client.py", "agent.md", "submission.yaml", "requirements.txt", "README.md"]


def test_vendored_template_matches_source() -> None:
    for name in SHARED_FILES:
        vendored = (VENDORED / name).read_text(encoding="utf-8")
        source = (SOURCE / name).read_text(encoding="utf-8")
        assert vendored == source, (
            f"{name} drifted between vendored template and submissions/python-template"
        )
