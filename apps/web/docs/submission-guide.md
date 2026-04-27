# Submission Guide

Submission bundles are executable agents. They must be non-interactive and support the arena command contract.

## Required Commands

```bash
./agent.py info --output agent-info.json
./agent.py generate --task task.md --data-dir data --output-dir output
./agent.py evaluate --task task.md --data-dir data --source-dir output/source --built-dir output/built --output evaluation.json
```

`generate` should create:

```text
output/
  source/
  built/
    index.html
  generation.json
```

`evaluate` should write a JSON report with score, rubric criteria, browser evidence, source observations, and artifact references.

## Evaluation Expectations

Evaluation should primarily use browser automation. Use Playwright to load `built/index.html`, inspect the DOM, test responsive viewports, capture screenshots, check console errors, and exercise interactions.

Source inspection should be reserved for behavior that is hard to confirm in the browser, such as animation timing or hidden data transforms.

## Template Agent

The reference bundle in `submissions/python-template/` is a compact OpenAI agent loop. It gives the model a bash tool and a Playwright tool. The model writes any browser scripts it needs during evaluation.

Do not include API keys in the ZIP.
