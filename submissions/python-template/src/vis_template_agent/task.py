from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class TaskDocument:
    path: Path
    metadata: dict[str, Any]
    body: str

    @property
    def task_id(self) -> str:
        return str(self.metadata.get("id", self.path.stem))

    @property
    def title(self) -> str:
        return str(self.metadata.get("title", self.task_id))

    @property
    def total_points(self) -> float:
        return float(self.metadata.get("rubric", {}).get("total_points", 100))

    @property
    def criteria(self) -> list[dict[str, Any]]:
        return list(self.metadata.get("rubric", {}).get("criteria", []))

    @property
    def viewport_sizes(self) -> list[tuple[int, int]]:
        raw_sizes = self.metadata.get("constraints", {}).get("viewport_sizes") or [[1440, 900]]
        sizes: list[tuple[int, int]] = []
        for size in raw_sizes:
            if isinstance(size, (list, tuple)) and len(size) == 2:
                sizes.append((int(size[0]), int(size[1])))
        return sizes or [(1440, 900)]


def load_task(path: str | Path) -> TaskDocument:
    task_path = Path(path)
    text = task_path.read_text(encoding="utf-8")
    metadata: dict[str, Any] = {}
    body = text
    if text.startswith("---\n"):
        _, frontmatter, body = text.split("---", 2)
        metadata = yaml.safe_load(frontmatter) or {}
        body = body.lstrip("\n")
    return TaskDocument(path=task_path, metadata=metadata, body=body)

