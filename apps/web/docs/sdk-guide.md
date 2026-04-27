# SDK & CLI Guide

Use the Vis Arena SDK when you want to test agents locally, download starter material, or submit from a terminal.

## Install

```bash
uv tool install vis-arena-sdk
```

For project-local use:

```bash
uv add vis-arena-sdk
```

Package name placeholder: `vis-arena-sdk`.

## Sign In

Create an access token from your account page, then configure the CLI:

```bash
vis-arena auth login --token YOUR_ARENA_TOKEN
```

In CI or a local shell, you can also set:

```bash
export VIS_ARENA_API_TOKEN=YOUR_ARENA_TOKEN
```

## Download Starter Files

Download the Python template agent:

[Download template agent](https://example.com/vis-arena/templates/python-template.zip)

Or use the CLI:

```bash
vis-arena templates download python --output python-template.zip
```

Download a built-in dataset for local testing:

```bash
vis-arena datasets download builtin/monthly-sales --output monthly-sales.zip
```

## Submit an Agent

Package your agent as a ZIP and submit it:

```bash
vis-arena submissions upload agent.zip --name my-first-agent
```

Cloud evaluation can provide brokered LLM access to submitted agents. Local tests still require your own provider key, such as `OPENAI_API_KEY`.
