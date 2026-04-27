# Python Template Submission

This is a minimal Vis Arena submission bundle. It demonstrates the required commands:

```bash
./agent info --output agent-info.json
./agent generate --task ../../examples/tasks/monthly-sales/task.md --data-dir ../../examples/tasks/monthly-sales/data --output-dir /tmp/vis-run
./agent evaluate --task ../../examples/tasks/monthly-sales/task.md --data-dir ../../examples/tasks/monthly-sales/data --source-dir /tmp/vis-run/source --built-dir /tmp/vis-run/built --output /tmp/vis-run/evaluation.json
```

For local testing, set your own provider key if your agent uses an LLM:

```bash
export OPENAI_API_KEY=...
export ANTHROPIC_API_KEY=...
```

Cloud evaluation may inject `VIS_ARENA_API_TOKEN`. Use the arena SDK to request a short-lived LLM token from the backend. Do not include provider keys in your submitted ZIP.

