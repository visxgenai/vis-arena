# Dataset Guide

Datasets are community-contributed ZIP bundles of visualization tasks and data.

## Task Format

Each task uses a Markdown file named `task.md`. The file should include YAML frontmatter for stable metadata and a Markdown body for the human-readable task.

```markdown
---
id: monthly-sales-v1
title: Monthly Sales Dashboard
version: 1
data:
  - path: data/sales.csv
    role: primary
rubric:
  total_points: 100
---

Build an interactive web visualization for the provided sales data.
```

## Data Files

Data may use any format the task explains clearly: CSV, JSON, images, SQLite, Parquet, or mixed assets. Keep paths relative to the task folder.

## Rubrics

Rubrics should describe what an evaluator should verify. Prefer concrete criteria such as data correctness, visual clarity, responsive layout, accessibility, and interaction quality.

## Upload Flow

The web app uploads dataset ZIPs through presigned S3 URLs. If a dataset does not appear after upload, confirm the ZIP includes at least one `task.md`.
