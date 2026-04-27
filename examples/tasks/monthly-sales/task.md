---
id: monthly-sales-v1
title: Monthly Sales Dashboard
version: 1
type: visualization
data:
  - path: sales.csv
    role: primary
    media_type: text/csv
    description: Monthly revenue, units, and region totals.
rubric:
  total_points: 100
  criteria:
    - id: correctness
      points: 35
      description: Accurately represents every monthly revenue value and preserves month ordering.
    - id: usability
      points: 25
      description: Includes readable labels, units, legends or direct labels, and useful hover/details.
    - id: visual_design
      points: 20
      description: Uses an appropriate chart type, visual hierarchy, spacing, and color contrast.
    - id: robustness
      points: 20
      description: Renders without overlap at desktop and mobile viewport sizes.
constraints:
  artifact_entrypoint: index.html
  viewport_sizes:
    - [1440, 900]
    - [390, 844]
evaluation:
  preferred_methods:
    - browser_automation
    - screenshot_analysis
    - source_inspection
---

Create a web visualization that helps a sales director understand monthly performance.

The visualization should show monthly revenue trends, indicate the strongest and weakest months, and provide enough context to compare units sold with revenue. The page must be self-contained and runnable as a static web artifact.

