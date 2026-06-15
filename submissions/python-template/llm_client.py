"""LLM call routing for the example agent.

How model calls are sent: locally via the OpenAI API (your OPENAI_API_KEY), or
in an arena cloud job via the arena broker (no provider key needed). Token usage
and per-job budgets are recorded and enforced server-side by the arena — editing
this file changes how requests are *sent*, not what the arena records.

You usually don't need to touch this; it lives here (separate from your agent
logic in example_agent.py) so you can see and customize the transport — retries,
streaming, timeouts — if you want. example_agent.py imports make_llm_client().
"""
from __future__ import annotations

import os
from typing import Any

from openai import OpenAI


class OpenAIChatClient:
    def __init__(self) -> None:
        self.client = OpenAI()

    def create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": 8192}
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        response = self.client.chat.completions.create(**kwargs)
        return response.choices[0].message.model_dump(exclude_none=True)


class ArenaChatClient:
    def __init__(self, purpose: str) -> None:
        from vis_arena_sdk import VisArenaClient

        self.job_id = os.environ["VIS_ARENA_JOB_ID"]
        self.purpose = purpose
        self.client = VisArenaClient(
            base_url=os.environ.get("VIS_ARENA_SERVER_URL", "http://host.docker.internal:8000"),
            token=os.environ["VIS_ARENA_API_TOKEN"],
            # Bedrock can take several minutes when returning a large
            # tool call that writes the final HTML artifact.
            timeout=600.0,
        )

    def create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] | None,
    ) -> dict[str, Any]:
        response = self.client.create_llm_message(
            job_id=self.job_id,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            model=model,
            purpose=self.purpose,
            max_tokens=8192,
        )
        return response.message


def make_llm_client(purpose: str) -> OpenAIChatClient | ArenaChatClient:
    # Cloud: the arena worker injects VIS_ARENA_API_TOKEN + VIS_ARENA_JOB_ID and
    # the agent routes model calls through the arena backend (no provider key
    # needed). Local: you set OPENAI_API_KEY.
    if os.environ.get("VIS_ARENA_API_TOKEN") and os.environ.get("VIS_ARENA_JOB_ID") and not os.environ.get("OPENAI_API_KEY"):
        return ArenaChatClient(purpose)
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit(
            "OPENAI_API_KEY is not set.\n"
            "  Local testing: export OPENAI_API_KEY=sk-... and re-run.\n"
            "  Submitting:   run `vis-arena submit .`; the arena provides cloud models."
        )
    return OpenAIChatClient()
