---
id: visualization-publications-v1
title: IEEE VIS Publications Explorer
version: 1
type: visualization
data:
  - path: IEEE VIS papers 1990-2024 - Main dataset.csv
    role: primary
    media_type: text/csv
    description: VisPubData metadata for IEEE VIS papers from 1990-2024, including venue, year, title, authors, affiliations, references, keywords, citation and download counts, awards, and graphics replicability stamps.
rubric:
  total_points: 100
  criteria:
    - id: correctness
      points: 35
      description: Accurately parses the CSV, preserves publication counts by year and venue, handles missing values without dropping valid records, and correctly represents citation, download, award, keyword, author, and replicability-stamp fields when used.
    - id: analytical_depth
      points: 25
      description: Reveals meaningful publication patterns across time, venues, paper types, topics, authorship, citations or downloads, and highlights notable papers or outliers.
    - id: usability
      points: 20
      description: Provides clear labels, legends, units, filtering or search where useful, and enough detail on demand to inspect individual papers, authors, venues, keywords, awards, DOI links, or abstracts.
    - id: visual_design
      points: 10
      description: Uses appropriate encodings, visual hierarchy, spacing, and color contrast for a dense scholarly-publication dataset.
    - id: robustness
      points: 10
      description: Renders without layout breakage or text overlap at desktop and mobile viewport sizes, including long paper titles, author lists, abstracts, and keyword strings.
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

Create a web visualization that helps visualization researchers explore how IEEE VIS publications have changed from 1990 through 2024.

The visualization should use the VisPubData main dataset to show publication volume over time and compare the four venue labels in the data: `Vis`, `InfoVis`, `SciVis`, and `VAST`. It should help users find important patterns such as venue growth, changes in paper types, highly cited or highly downloaded papers, award-winning papers, graphics replicability stamps, prolific authors, recurring keywords, and citation relationships between VIS papers. The page must include a way to inspect individual papers with their title, year, venue, authors, abstract, DOI or link, and relevant metrics.

The data contains multi-value fields separated by semicolons, including authors, affiliations, internal references, and some keyword values. Empty cells should be treated as missing data rather than zeros unless the column semantics make zero explicit. The page must be self-contained and runnable as a static web artifact.

Dataset source: VisPubData, "Visualization publications dataset", https://sites.google.com/site/vispubdata/home.
