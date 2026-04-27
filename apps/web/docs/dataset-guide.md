# Dataset Guide

Vis Arena includes built-in datasets for normal submissions. You only need this guide if you want to create additional datasets for local experiments or community benchmarks.

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

## Local Use

Download a built-in dataset:

```bash
vis-arena datasets download builtin/monthly-sales --output monthly-sales.zip
```

To share a new dataset, package one or more task folders in a ZIP and upload it from the web app.
