from __future__ import annotations

import csv
import html
import json
from datetime import UTC, datetime
from pathlib import Path

from .task import TaskDocument


def generate(task: TaskDocument, data_dir: Path, output_dir: Path) -> dict:
    source_dir = output_dir / "source"
    built_dir = output_dir / "built"
    source_dir.mkdir(parents=True, exist_ok=True)
    built_dir.mkdir(parents=True, exist_ok=True)

    data_refs = task.metadata.get("data", [])
    primary = data_refs[0]["path"] if data_refs else "sales.csv"
    rows = _read_csv(data_dir / primary)

    source_html = _render_html(task, rows)
    (source_dir / "index.html").write_text(source_html, encoding="utf-8")
    (built_dir / "index.html").write_text(source_html, encoding="utf-8")

    report = {
        "schema_version": "vis-arena.generation.v1",
        "task_id": task.task_id,
        "entrypoint": "index.html",
        "source_dir": "source",
        "built_dir": "built",
        "created_at": datetime.now(UTC).isoformat(),
        "notes": "Template agent generated a static SVG bar chart from the primary CSV file."
    }
    (output_dir / "generation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _render_html(task: TaskDocument, rows: list[dict[str, str]]) -> str:
    values = [float(row.get("revenue", "0") or 0) for row in rows]
    max_value = max(values or [1])
    bars = []
    labels = []
    for index, row in enumerate(rows):
        revenue = float(row.get("revenue", "0") or 0)
        height = 260 * revenue / max_value
        x = 56 + index * 58
        y = 310 - height
        month = html.escape(row.get("month", ""))
        units = html.escape(row.get("units", ""))
        bars.append(
            f'<rect class="bar" x="{x}" y="{y:.1f}" width="34" height="{height:.1f}" '
            f'data-month="{month}" data-revenue="{revenue:.0f}" data-units="{units}"><title>{month}: ${revenue:,.0f}, {units} units</title></rect>'
        )
        labels.append(f'<text class="month" x="{x + 17}" y="336">{month[-2:]}</text>')

    strongest = rows[values.index(max_value)] if rows else {}
    weakest_value = min(values or [0])
    weakest = rows[values.index(weakest_value)] if rows else {}

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(task.title)}</title>
  <style>
    :root {{ color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; background: #f7f7f4; color: #1f2933; }}
    main {{ max-width: 980px; margin: 0 auto; padding: 32px 20px 40px; }}
    h1 {{ margin: 0 0 8px; font-size: 30px; line-height: 1.15; }}
    .subtitle {{ margin: 0 0 24px; color: #52606d; }}
    .panel {{ background: #ffffff; border: 1px solid #d9e2ec; border-radius: 8px; padding: 20px; box-shadow: 0 8px 18px rgba(31, 41, 51, 0.06); }}
    svg {{ display: block; width: 100%; height: auto; overflow: visible; }}
    .axis {{ stroke: #9fb3c8; stroke-width: 1; }}
    .bar {{ fill: #2f80ed; transition: fill 160ms ease; }}
    .bar:hover {{ fill: #0b5cad; }}
    .month {{ font-size: 12px; text-anchor: middle; fill: #52606d; }}
    .metric-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 18px; }}
    .metric {{ border-left: 4px solid #2f80ed; padding: 10px 12px; background: #f5f8fb; }}
    .metric strong {{ display: block; font-size: 14px; color: #52606d; }}
    .metric span {{ display: block; margin-top: 4px; font-size: 18px; font-weight: 700; }}
    @media (max-width: 560px) {{
      main {{ padding: 20px 12px 28px; }}
      h1 {{ font-size: 23px; }}
      .panel {{ padding: 14px; }}
      .metric-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>{html.escape(task.title)}</h1>
    <p class="subtitle">Monthly revenue with hover details for units sold.</p>
    <section class="panel" aria-label="Monthly revenue chart">
      <svg viewBox="0 0 780 370" role="img" aria-labelledby="chart-title chart-desc">
        <title id="chart-title">Monthly revenue bar chart</title>
        <desc id="chart-desc">Bars show revenue for each month in the supplied dataset.</desc>
        <line class="axis" x1="44" y1="310" x2="748" y2="310"></line>
        <line class="axis" x1="44" y1="42" x2="44" y2="310"></line>
        {"".join(bars)}
        {"".join(labels)}
      </svg>
      <div class="metric-grid">
        <div class="metric"><strong>Strongest month</strong><span>{html.escape(str(strongest.get("month", "n/a")))} · ${max_value:,.0f}</span></div>
        <div class="metric"><strong>Weakest month</strong><span>{html.escape(str(weakest.get("month", "n/a")))} · ${weakest_value:,.0f}</span></div>
      </div>
    </section>
  </main>
</body>
</html>
"""

