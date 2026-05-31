from __future__ import annotations

import json
import time
import uuid
from typing import Any

import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException

from .db import connect, now_iso, row_to_dict
from .schemas import LLMMessageRequest
from .settings import settings


def create_llm_message(payload: LLMMessageRequest, user_id: str) -> dict[str, Any]:
    if not settings.cloud_llm_enabled:
        raise HTTPException(
            status_code=403,
            detail="Cloud LLM brokerage is disabled in this deployment. Use your own provider keys for local testing.",
        )
    if settings.llm_provider != "bedrock":
        raise HTTPException(status_code=400, detail="Cloud message brokerage is only configured for Bedrock")

    context = _job_context(payload.job_id, user_id)
    used_tokens = _submission_token_total(context["submission_id"])
    if used_tokens >= settings.llm_max_tokens_per_submission:
        raise HTTPException(status_code=429, detail="Submission LLM token budget exhausted")

    remaining_tokens = settings.llm_max_tokens_per_submission - used_tokens
    max_tokens = max(1, min(payload.max_tokens, remaining_tokens))
    started = time.monotonic()
    bedrock = _invoke_bedrock(payload, max_tokens)
    latency_ms = int((time.monotonic() - started) * 1000)

    usage = bedrock["usage"]
    total_tokens = usage["input_tokens"] + usage["output_tokens"]
    estimated_cost = _estimated_cost_usd(usage["input_tokens"], usage["output_tokens"])
    _record_usage(
        job_id=payload.job_id,
        submission_id=context["submission_id"],
        user_id=user_id,
        provider="bedrock",
        model_id=bedrock["model"],
        purpose=payload.purpose,
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        total_tokens=total_tokens,
        estimated_cost_usd=estimated_cost,
        latency_ms=latency_ms,
    )

    return {
        "provider": "bedrock",
        "model": bedrock["model"],
        "message": bedrock["message"],
        "usage": usage,
        "remaining_submission_tokens": max(0, remaining_tokens - total_tokens),
    }


def _job_context(job_id: str, user_id: str) -> dict[str, Any]:
    with connect() as db:
        row = db.execute(
            """
            select jobs.id, jobs.submission_id, jobs.status, submissions.owner_id
            from jobs join submissions on submissions.id = jobs.submission_id
            where jobs.id = ?
            """,
            (job_id,),
        ).fetchone()
    context = row_to_dict(row)
    if not context or context["owner_id"] != user_id:
        raise HTTPException(status_code=404, detail="Job not found")
    if context["status"] != "running":
        raise HTTPException(status_code=400, detail="LLM calls are only allowed while the job is running")
    return context


def _submission_token_total(submission_id: str) -> int:
    with connect() as db:
        row = db.execute("select coalesce(sum(total_tokens), 0) as total from llm_usage where submission_id = ?", (submission_id,)).fetchone()
    return int(row["total"] or 0)


def _invoke_bedrock(payload: LLMMessageRequest, max_tokens: int) -> dict[str, Any]:
    model_id = _resolve_bedrock_model(payload.model)
    body = _anthropic_body(payload.messages, payload.tools, payload.tool_choice, max_tokens)
    client = boto3.client("bedrock-runtime", region_name=settings.bedrock_region)
    try:
        response = client.invoke_model(
            modelId=model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body),
        )
    except ClientError as exc:
        message = exc.response.get("Error", {}).get("Message") or str(exc)
        raise HTTPException(status_code=502, detail=f"Bedrock invoke failed: {message}") from exc
    raw = json.loads(response["body"].read())
    usage = raw.get("usage") or {}
    return {
        "model": raw.get("model") or model_id,
        "message": _openai_message_from_anthropic(raw.get("content") or []),
        "usage": {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "total_tokens": int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0),
        },
    }


def _resolve_bedrock_model(requested_model: str | None) -> str:
    model_id = requested_model or settings.bedrock_default_model_id
    if not model_id:
        raise HTTPException(status_code=500, detail="No Bedrock models are configured for this arena")
    if model_id not in settings.bedrock_model_ids:
        raise HTTPException(status_code=400, detail=f"Model is not enabled for this arena: {model_id}")
    return model_id


def _anthropic_body(messages: list[dict[str, Any]], tools: list[dict[str, Any]], tool_choice: str | dict[str, Any] | None, max_tokens: int) -> dict[str, Any]:
    system_parts: list[str] = []
    bedrock_messages: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            content = message.get("content")
            if content:
                system_parts.append(str(content))
            continue
        if role == "tool":
            bedrock_messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.get("tool_call_id"),
                            "content": str(message.get("content") or ""),
                        }
                    ],
                }
            )
            continue
        if role == "assistant":
            bedrock_messages.append({"role": "assistant", "content": _assistant_content(message)})
            continue
        bedrock_messages.append({"role": "user", "content": _text_content(message.get("content"))})

    body: dict[str, Any] = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": bedrock_messages,
    }
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    converted_tools = [_anthropic_tool(tool) for tool in tools]
    if converted_tools:
        body["tools"] = converted_tools
        if tool_choice:
            body["tool_choice"] = {"type": "auto"} if tool_choice == "auto" else tool_choice
    return body


def _text_content(value: Any) -> list[dict[str, str]]:
    if isinstance(value, list):
        text = "\n".join(str(item.get("text") if isinstance(item, dict) else item) for item in value)
    else:
        text = str(value or "")
    return [{"type": "text", "text": text}]


def _assistant_content(message: dict[str, Any]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    if message.get("content"):
        content.extend(_text_content(message["content"]))
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        raw_arguments = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            arguments = {}
        content.append(
            {
                "type": "tool_use",
                "id": call.get("id"),
                "name": function.get("name"),
                "input": arguments,
            }
        )
    return content or [{"type": "text", "text": ""}]


def _anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
    function = tool.get("function") or tool
    return {
        "name": function["name"],
        "description": function.get("description", ""),
        "input_schema": function.get("parameters", {"type": "object", "properties": {}}),
    }


def _openai_message_from_anthropic(content: list[dict[str, Any]]) -> dict[str, Any]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in content:
        if item.get("type") == "text":
            text_parts.append(item.get("text") or "")
        elif item.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": item.get("id"),
                    "type": "function",
                    "function": {
                        "name": item.get("name"),
                        "arguments": json.dumps(item.get("input") or {}),
                    },
                }
            )
    message: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _record_usage(
    *,
    job_id: str,
    submission_id: str,
    user_id: str,
    provider: str,
    model_id: str,
    purpose: str,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    estimated_cost_usd: float | None,
    latency_ms: int,
) -> None:
    with connect() as db:
        db.execute(
            """
            insert into llm_usage (
              id, job_id, submission_id, user_id, provider, model_id, purpose,
              input_tokens, output_tokens, total_tokens, estimated_cost_usd,
              latency_ms, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                job_id,
                submission_id,
                user_id,
                provider,
                model_id,
                purpose,
                input_tokens,
                output_tokens,
                total_tokens,
                estimated_cost_usd,
                latency_ms,
                now_iso(),
            ),
        )


def _estimated_cost_usd(input_tokens: int, output_tokens: int) -> float | None:
    if settings.llm_input_usd_per_1m <= 0 and settings.llm_output_usd_per_1m <= 0:
        return None
    return (input_tokens / 1_000_000 * settings.llm_input_usd_per_1m) + (output_tokens / 1_000_000 * settings.llm_output_usd_per_1m)
