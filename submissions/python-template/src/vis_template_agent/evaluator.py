from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .task import TaskDocument


def evaluate(task: TaskDocument, data_dir: Path, source_dir: Path, built_dir: Path, output_path: Path) -> dict[str, Any]:
    screenshots_dir = output_path.parent / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    browser_report = _run_playwright_checks(task, built_dir, screenshots_dir)
    source_observations = _inspect_source(source_dir, built_dir)
    criteria = _score_criteria(task, browser_report, source_observations)
    score = sum(item["score"] for item in criteria)

    report = {
        "schema_version": "vis-arena.evaluation.v1",
        "task_id": task.task_id,
        "score": round(score, 2),
        "max_score": task.total_points,
        "summary": _summary(score, task.total_points, browser_report),
        "criteria": criteria,
        "browser": browser_report,
        "source_observations": source_observations,
        "artifacts": {
            "screenshots": [str(path.relative_to(output_path.parent)) for path in screenshots_dir.glob("*.png")],
            "logs": []
        },
        "metadata": {
            "evaluated_at": datetime.now(UTC).isoformat(),
            "evaluator": "python-template",
            "llm_access": _llm_access_note()
        }
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def _run_playwright_checks(task: TaskDocument, built_dir: Path, screenshots_dir: Path) -> dict[str, Any]:
    entrypoint = (built_dir / "index.html").resolve()
    script = _playwright_script(entrypoint, task.viewport_sizes, screenshots_dir.resolve())
    with tempfile.NamedTemporaryFile("w", suffix=".py", encoding="utf-8", delete=False) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        result = subprocess.run(
            ["python", str(script_path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=45,
            check=False,
        )
    finally:
        script_path.unlink(missing_ok=True)

    if result.returncode != 0:
        return {
            "tool": "playwright",
            "entrypoint_url": entrypoint.as_uri(),
            "available": False,
            "error": result.stdout[-2000:],
            "viewports": []
        }
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {"available": False, "error": result.stdout[-2000:], "viewports": []}
    payload.setdefault("tool", "playwright")
    payload.setdefault("entrypoint_url", entrypoint.as_uri())
    return payload


def _playwright_script(entrypoint: Path, viewports: list[tuple[int, int]], screenshots_dir: Path) -> str:
    sizes = [{"width": width, "height": height} for width, height in viewports]
    return f'''
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

entrypoint = {str(entrypoint.as_uri())!r}
screenshots_dir = Path({str(screenshots_dir)!r})
sizes = {sizes!r}
report = {{"available": True, "viewports": []}}

with sync_playwright() as p:
    browser = p.chromium.launch()
    for size in sizes:
        page = browser.new_page(viewport=size)
        console_errors = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.goto(entrypoint)
        page.wait_for_load_state("networkidle")
        bars = page.locator(".bar").count()
        svg = page.locator("svg").count()
        title = page.title()
        screenshot = screenshots_dir / f"{{size['width']}}x{{size['height']}}.png"
        page.screenshot(path=str(screenshot), full_page=True)
        overlaps = page.evaluate("""
        () => {{
          const visible = [...document.querySelectorAll('body *')].filter(el => {{
            const r = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
          }});
          let count = 0;
          for (let i = 0; i < visible.length; i++) {{
            const a = visible[i].getBoundingClientRect();
            for (let j = i + 1; j < visible.length; j++) {{
              const b = visible[j].getBoundingClientRect();
              const area = Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left)) *
                Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top));
              if (area > 5000) count++;
            }}
          }}
          return count;
        }}
        """)
        report["viewports"].append({{
            "width": size["width"],
            "height": size["height"],
            "checks": [f"title={{title}}", f"svg_count={{svg}}", f"bar_count={{bars}}", f"console_errors={{len(console_errors)}}", f"large_overlap_count={{overlaps}}"],
            "screenshot": str(screenshot),
            "bar_count": bars,
            "svg_count": svg,
            "console_errors": console_errors,
            "large_overlap_count": overlaps
        }})
        page.close()
    browser.close()

print(json.dumps(report))
'''


def _inspect_source(source_dir: Path, built_dir: Path) -> list[str]:
    observations: list[str] = []
    files = list(source_dir.rglob("*")) + list(built_dir.rglob("*"))
    html_files = [path for path in files if path.suffix.lower() in {".html", ".css", ".js"} and path.is_file()]
    observations.append(f"Inspected {len(html_files)} source or built web files.")
    text = "\n".join(path.read_text(encoding="utf-8", errors="replace")[:20000] for path in html_files)
    if "@media" in text:
        observations.append("Responsive CSS media query found.")
    if "aria-" in text or "role=\"img\"" in text:
        observations.append("Accessibility metadata found.")
    if "transition" in text or "animation" in text:
        observations.append("Motion styling found in source.")
    return observations


def _score_criteria(task: TaskDocument, browser_report: dict[str, Any], source_observations: list[str]) -> list[dict[str, Any]]:
    criteria = task.criteria or [
        {"id": "correctness", "points": 35, "description": ""},
        {"id": "usability", "points": 25, "description": ""},
        {"id": "visual_design", "points": 20, "description": ""},
        {"id": "robustness", "points": 20, "description": ""},
    ]
    browser_available = bool(browser_report.get("available"))
    viewports = browser_report.get("viewports", [])
    max_bars = max([vp.get("bar_count", 0) for vp in viewports] or [0])
    console_errors = sum(len(vp.get("console_errors", [])) for vp in viewports)
    overlaps = sum(int(vp.get("large_overlap_count", 0)) for vp in viewports)
    has_responsive_css = any("Responsive" in item for item in source_observations)
    has_accessibility = any("Accessibility" in item for item in source_observations)

    scored: list[dict[str, Any]] = []
    for item in criteria:
        criterion_id = str(item["id"])
        max_score = float(item["points"])
        evidence: list[str] = []
        ratio = 0.55
        if criterion_id == "correctness":
            ratio = 0.85 if max_bars >= 8 else 0.45
            evidence.append(f"Browser observed up to {max_bars} data bars.")
        elif criterion_id == "usability":
            ratio = 0.75 if has_accessibility else 0.55
            evidence.append("Accessibility labels were present." if has_accessibility else "Accessibility labels were limited.")
        elif criterion_id == "visual_design":
            ratio = 0.7 if browser_available else 0.45
            evidence.append("Rendered artifact was inspected in browser." if browser_available else "Browser inspection was unavailable.")
        elif criterion_id == "robustness":
            ratio = 0.85 if browser_available and console_errors == 0 and overlaps < 8 and has_responsive_css else 0.5
            evidence.append(f"Console errors: {console_errors}; large overlap count: {overlaps}; responsive CSS: {has_responsive_css}.")
        else:
            evidence.append("Template evaluator applied a neutral heuristic.")
        scored.append({
            "id": criterion_id,
            "score": round(max_score * ratio, 2),
            "max_score": max_score,
            "evidence": evidence
        })
    return scored


def _summary(score: float, max_score: float, browser_report: dict[str, Any]) -> str:
    if not browser_report.get("available"):
        return "Evaluation completed with source heuristics because Playwright was unavailable or failed."
    percentage = 100 * score / max_score if max_score else 0
    return f"Template browser evaluation completed with a heuristic score of {percentage:.1f}%."


def _llm_access_note() -> str:
    return (
        "Local runs should use participant-owned provider keys. Cloud runs may expose VIS_ARENA_API_TOKEN "
        "for requesting short-lived arena-brokered LLM credentials."
    )

