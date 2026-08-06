# QA central judge (mechanism)

The arena's central judge grades an artifact by whether it **answers grounded,
factual questions from the rendered visualization alone** — visible text,
charts, and interactions; never page source or embedded raw data. Score =
fraction of questions answered correctly.

This directory is the **public mechanism only**:

- `example_agent.py` — question loading, the bounded answer-collection loop,
  normalization, and grading (pure functions, unit-tested).
- `questions.example.json` — a dummy fixture documenting the schema.

The **real question set is private**. It is injected as `questions.json` when
the judge bundle is built and is never committed here. The judge's public
output is aggregate-only (score + per-question-id correctness); the public
evaluation-report endpoint does not serve central-judge reports.

**Two aspects, one inspection pass:** the judge (1) answers the private grounded
questions from the rendered page (answers must be supported by visual evidence,
not prose alone), and (2) rates the arena's five public storytelling criteria.
Default combination is a **direct sum: QA (0-100) + rubric (0-100), max 200**;
a weighted mode (`combine: "weighted"` + `qa_weight`) exists as the alternative.

**The container never holds the answer key.** Cloud bundles carry questions
only; the judge returns raw answers, rubric ratings, screenshots, and
mechanical render stats, and the SERVER grades them against a privately stored
key. A compromised or prompt-injected judge therefore cannot read or leak
answers — it can only emit wrong ones.
