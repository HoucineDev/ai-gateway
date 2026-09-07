"""OpenAI-compatible adapter: vLLM, KServe (OpenAI protocol), TGI, Ollama, any /v1 server, and OpenAI itself.

Our own translation; no third-party gateway code. docs/spec/03 §4 (openai_compat column).
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from aigw.adapters.base import BaseHTTPAdapter, Capabilities, DeploymentConfig
from aigw.core.errors import ErrorClass, UpstreamError
from aigw.core.types import (
    ChatEvent,
    ChatRequest,
    ChatResponse,
    Choice,
    DeltaEvent,
    Embedding,
    EmbeddingRequest,
    EmbeddingResponse,
    EndEvent,
    FinishEvent,
    FunctionCall,
    Message,
    StartEvent,
    ToolCall,
    ToolCallDelta,
    Usage,
    UsageEvent,
)

_COMMON_PARAMS = {
    "max_tokens",
    "max_completion_tokens",
    "temperature",
    "top_p",
    "stop",
    "n",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "response_format",
    "seed",
    "presence_penalty",
    "frequency_penalty",
    "logprobs",
    "top_logprobs",
    "stream_options",
    "encoding_format",
    "dimensions",
}


class OpenAICompatAdapter(BaseHTTPAdapter):
    provider = "openai_compat"
    default_caps = Capabilities(
        endpoints={"chat", "embeddings"},
        streaming=True,
        tools=True,
        parallel_tools=True,
        json_object=True,
        json_schema=True,
        vision=True,
        system_role="message",
        reports_usage_in_stream=True,
        provider_extensions_passthrough=True,  # local servers commonly expose extra sampling params
        supported_params=set(_COMMON_PARAMS),
    )
    default_base_url = "http://localhost:8000/v1"
    max_tokens_field = "max_tokens"

    # -- helpers ------------------------------------------------------------
    def _url(self, deployment: DeploymentConfig, path: str) -> str:
        base = (deployment.base_url or self.default_base_url).rstrip("/")
        return f"{base}{path}"

    def _headers(self, deployment: DeploymentConfig, credential: str) -> dict[str, str]:
        h = {"content-type": "application/json", **(deployment.extra_headers or {})}
        if credential:
            h["authorization"] = f"Bearer {credential}"
        return h

    def _payload(self, req: ChatRequest, deployment: DeploymentConfig) -> dict[str, Any]:
        body = req.model_dump(
            exclude_none=True,
            exclude={"model", "metadata", "extra_body", "max_completion_tokens", "stream_options"},
            by_alias=True,
        )
        body["model"] = deployment.provider_model
        if req.max_tokens:
            body.pop("max_tokens", None)
            body[self.max_tokens_field] = req.max_tokens
        if req.stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        if req.extra_body:
            body.update(req.extra_body)
        return body

    # -- chat ---------------------------------------------------------------
    async def chat(self, req: ChatRequest, deployment: DeploymentConfig, credential: str) -> ChatResponse:
        data, headers = await self._post_json(
            self._url(deployment, "/chat/completions"),
            self._payload(req, deployment),
            self._headers(deployment, credential),
            deployment,
        )
        return self._parse_response(data, headers.get("x-request-id"))

    def _parse_response(self, data: dict, rid: str | None) -> ChatResponse:
        choices = []
        for c in data.get("choices", []):
            m = c.get("message", {})
            tool_calls = None
            if m.get("tool_calls"):
                tool_calls = [
                    ToolCall(
                        id=t.get("id") or f"call_{i}",
                        function=FunctionCall(
                            name=t["function"]["name"], arguments=t["function"].get("arguments") or ""
                        ),
                    )
                    for i, t in enumerate(m["tool_calls"])
                ]
            choices.append(
                Choice(
                    index=c.get("index", 0),
                    message=Message(role="assistant", content=m.get("content"), tool_calls=tool_calls),
                    finish_reason=_finish(c.get("finish_reason")),
                )
            )
        usage = _usage(data.get("usage"))
        return ChatResponse(
            id=data.get("id") or "chatcmpl",
            model=data.get("model", ""),
            choices=choices,
            usage=usage,
            created=data.get("created") or int(time.time()),
            upstream_request_id=rid,
            system_fingerprint=data.get("system_fingerprint"),
        )

    async def chat_stream(
        self, req: ChatRequest, deployment: DeploymentConfig, credential: str
    ) -> AsyncIterator[ChatEvent]:
        started = False
        done = False
        finished_indexes: set[int] = set()
        usage_sent = False
        async for _event, data, headers in self._sse(
            self._url(deployment, "/chat/completions"),
            self._payload(req, deployment),
            self._headers(deployment, credential),
            deployment,
        ):
            if not started:
                yield StartEvent(upstream_request_id=headers.get("x-request-id"))
                started = True
            if data.strip() == "[DONE]":
                done = True
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if "error" in chunk and not chunk.get("choices"):
                raise UpstreamError(
                    ErrorClass.upstream_error,
                    str(chunk["error"].get("message", chunk["error"])),
                    provider=self.provider,
                    body=data,
                )
            for c in chunk.get("choices") or []:
                idx = c.get("index", 0)
                d = c.get("delta") or {}
                tool_deltas = None
                if d.get("tool_calls"):
                    tool_deltas = [
                        ToolCallDelta(
                            index=t.get("index", i),
                            id=t.get("id"),
                            function=FunctionCall(
                                name=t.get("function", {}).get("name") or "",
                                arguments=t.get("function", {}).get("arguments") or "",
                            )
                            if t.get("function")
                            else None,
                        )
                        for i, t in enumerate(d["tool_calls"])
                    ]
                if d.get("content") is not None or d.get("role") or tool_deltas:
                    yield DeltaEvent(index=idx, role=d.get("role"), content=d.get("content"), tool_calls=tool_deltas)
                if c.get("finish_reason") and idx not in finished_indexes:
                    finished_indexes.add(idx)
                    yield FinishEvent(index=idx, finish_reason=_finish(c["finish_reason"]) or "stop")
            if chunk.get("usage"):
                u = _usage(chunk["usage"])
                if u and not usage_sent:
                    usage_sent = True
                    yield UsageEvent(usage=u)
        if not started:
            raise UpstreamError(ErrorClass.upstream_error, "empty stream from upstream", provider=self.provider)
        if not done and not finished_indexes:
            # stream ended without [DONE] or a finish_reason: abnormal termination (docs/spec/03 §3)
            raise UpstreamError(
                ErrorClass.ambiguous, "upstream stream ended without completion", provider=self.provider
            )
        yield EndEvent()

    # -- embeddings ---------------------------------------------------------
    async def embeddings(
        self, req: EmbeddingRequest, deployment: DeploymentConfig, credential: str
    ) -> EmbeddingResponse:
        body = req.model_dump(exclude_none=True, exclude={"model", "metadata", "extra_body"})
        body["model"] = deployment.provider_model
        if req.extra_body:
            body.update(req.extra_body)
        data, headers = await self._post_json(
            self._url(deployment, "/embeddings"), body, self._headers(deployment, credential), deployment
        )
        return EmbeddingResponse(
            model=data.get("model", deployment.provider_model),
            data=[
                Embedding(index=e.get("index", i), embedding=e["embedding"]) for i, e in enumerate(data.get("data", []))
            ],
            usage=_usage(data.get("usage")),
            upstream_request_id=headers.get("x-request-id"),
        )


def _finish(reason: str | None):
    if reason in ("stop", "length", "tool_calls", "content_filter"):
        return reason
    if reason == "function_call":
        return "tool_calls"
    if reason in (None, "", "eos_token"):
        return "stop" if reason else None
    return "stop"


def _usage(u: dict | None) -> Usage | None:
    if not u:
        return None
    return Usage(
        prompt_tokens=int(u.get("prompt_tokens") or 0),
        completion_tokens=int(u.get("completion_tokens") or 0),
        total_tokens=int(u.get("total_tokens") or 0),
        cached_tokens=int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
        reasoning_tokens=int((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0),
    )
