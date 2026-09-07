"""Provider adapter contract (docs/spec/03)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel

from aigw.core.errors import ErrorClass, UnsupportedParameter, UpstreamError
from aigw.core.tokens import estimate_chat_prompt_tokens, estimate_embedding_tokens
from aigw.core.types import ChatEvent, ChatRequest, ChatResponse, EmbeddingRequest, EmbeddingResponse


@dataclass
class DeploymentConfig:
    """Immutable view of a deployment row handed to adapters."""

    id: str
    model_id: str
    model_name: str
    name: str
    provider: str
    provider_model: str
    base_url: str | None
    credential_ref: str
    weight: int = 1
    priority: int = 0
    capabilities: dict[str, Any] = field(default_factory=dict)
    extra_headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float | None = None
    max_input_tokens: int | None = None
    region: str | None = None
    cooldown_until: float | None = None  # epoch seconds (operator cooldown)


class Capabilities(BaseModel):
    endpoints: set[Literal["chat", "embeddings"]] = {"chat"}
    streaming: bool = True
    tools: bool = True
    parallel_tools: bool = True
    json_object: bool = True
    json_schema: bool = False
    vision: bool = False
    system_role: Literal["message", "top_level", "none"] = "message"
    max_context_tokens: int | None = None
    default_max_tokens: int | None = None
    reports_usage_in_stream: bool = True
    provider_extensions_passthrough: bool = False
    supported_params: set[str] = set()

    def narrowed(self, overrides: dict[str, Any]) -> Capabilities:
        """Apply deployment JSONB overrides. Booleans can only be narrowed (True→False); sets intersect."""
        data = self.model_dump()
        for k, v in overrides.items():
            if k not in data:
                continue
            cur = data[k]
            if isinstance(cur, bool):
                data[k] = cur and bool(v)
            elif isinstance(cur, set):
                data[k] = cur & set(v)
            elif k in ("max_context_tokens", "max_input_tokens") and cur is not None and v is not None:
                data[k] = min(cur, int(v))
            else:
                data[k] = v
        return Capabilities(**data)


class ProviderAdapter(Protocol):
    provider: str

    def capabilities(self, deployment: DeploymentConfig) -> Capabilities: ...

    def validate(self, req: ChatRequest | EmbeddingRequest, caps: Capabilities) -> None: ...

    async def chat(self, req: ChatRequest, deployment: DeploymentConfig, credential: str) -> ChatResponse: ...

    def chat_stream(
        self, req: ChatRequest, deployment: DeploymentConfig, credential: str
    ) -> AsyncIterator[ChatEvent]: ...

    async def embeddings(
        self, req: EmbeddingRequest, deployment: DeploymentConfig, credential: str
    ) -> EmbeddingResponse: ...

    def estimate_prompt_tokens(self, req: ChatRequest | EmbeddingRequest) -> int: ...


class BaseHTTPAdapter:
    """Shared plumbing: HTTP client, validation, error classification, SSE parsing."""

    provider: str = "base"
    default_caps: Capabilities = Capabilities()

    def __init__(self, client: httpx.AsyncClient, default_timeout: float = 120.0, connect_timeout: float = 10.0):
        self.client = client
        self.default_timeout = default_timeout
        self.connect_timeout = connect_timeout

    # -- capabilities -------------------------------------------------------
    def capabilities(self, deployment: DeploymentConfig) -> Capabilities:
        caps = self.default_caps
        if deployment.max_input_tokens:
            caps = caps.model_copy(update={"max_context_tokens": deployment.max_input_tokens})
        return caps.narrowed(deployment.capabilities or {})

    def validate(self, req: ChatRequest | EmbeddingRequest, caps: Capabilities) -> None:
        for p in sorted(req.set_params()):
            if p == "extra_body":
                if not caps.provider_extensions_passthrough:
                    raise UnsupportedParameter("extra_body", self.provider)
                continue
            if p not in caps.supported_params:
                raise UnsupportedParameter(p, self.provider)
        if isinstance(req, ChatRequest):
            if req.wants_tools() and not caps.tools:
                raise UnsupportedParameter("tools", self.provider)
            if req.wants_vision() and not caps.vision:
                raise UnsupportedParameter("messages.content.image_url", self.provider)
            if req.response_format:
                if req.response_format.type == "json_schema" and not caps.json_schema:
                    raise UnsupportedParameter("response_format.json_schema", self.provider)
                if req.response_format.type == "json_object" and not caps.json_object:
                    raise UnsupportedParameter("response_format.json_object", self.provider)
            if req.stream and not caps.streaming:
                raise UnsupportedParameter("stream", self.provider)

    def estimate_prompt_tokens(self, req: ChatRequest | EmbeddingRequest) -> int:
        if isinstance(req, ChatRequest):
            return estimate_chat_prompt_tokens(req)
        return estimate_embedding_tokens(req)

    # -- http ---------------------------------------------------------------
    def _timeout(self, deployment: DeploymentConfig) -> httpx.Timeout:
        total = deployment.timeout_seconds or self.default_timeout
        return httpx.Timeout(total, connect=self.connect_timeout, read=total, write=30.0, pool=self.connect_timeout)

    def _classify_status(self, status: int, body: str, headers: httpx.Headers) -> UpstreamError:
        rid = headers.get("x-request-id") or headers.get("request-id")
        retry_after = None
        if headers.get("retry-after"):
            try:
                retry_after = float(headers["retry-after"])
            except ValueError:
                retry_after = None
        msg = _extract_message(body) or f"upstream returned HTTP {status}"
        if status in (401, 403):
            cls = ErrorClass.authentication
        elif status == 429:
            cls = ErrorClass.rate_limited
        elif status in (503, 529):
            cls = ErrorClass.overloaded
        elif status == 408:
            cls = ErrorClass.timeout_before_send
        elif 400 <= status < 500:
            cls = (
                ErrorClass.content_filter
                if "content_filter" in body or "moderation" in body
                else ErrorClass.invalid_request
            )
        else:
            cls = ErrorClass.upstream_error
        return UpstreamError(
            cls,
            msg,
            provider=self.provider,
            status_code=status,
            upstream_request_id=rid,
            retry_after=retry_after,
            body=body,
        )

    def _classify_exception(self, exc: Exception, *, after_send: bool) -> UpstreamError:
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
            return UpstreamError(ErrorClass.timeout_before_send, f"connect failure: {exc}", provider=self.provider)
        if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError, httpx.ReadError)):
            cls = ErrorClass.ambiguous if after_send else ErrorClass.timeout_before_send
            return UpstreamError(cls, f"upstream {type(exc).__name__}: {exc}", provider=self.provider)
        return UpstreamError(ErrorClass.upstream_error, f"{type(exc).__name__}: {exc}", provider=self.provider)

    async def _post_json(
        self, url: str, payload: dict, headers: dict, deployment: DeploymentConfig
    ) -> tuple[dict, httpx.Headers]:
        try:
            resp = await self.client.post(url, json=payload, headers=headers, timeout=self._timeout(deployment))
        except httpx.HTTPError as exc:
            raise self._classify_exception(exc, after_send=False) from exc
        if resp.status_code >= 400:
            raise self._classify_status(resp.status_code, resp.text, resp.headers)
        try:
            return resp.json(), resp.headers
        except ValueError as exc:
            raise UpstreamError(
                ErrorClass.upstream_error,
                "upstream returned non-JSON body",
                provider=self.provider,
                status_code=resp.status_code,
                body=resp.text,
            ) from exc

    async def _sse(
        self, url: str, payload: dict, headers: dict, deployment: DeploymentConfig
    ) -> AsyncIterator[tuple[str, str, httpx.Headers]]:
        """Yield (event, data, response_headers) tuples from an SSE stream, with error classification."""
        try:
            async with self.client.stream(
                "POST", url, json=payload, headers=headers, timeout=self._timeout(deployment)
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    raise self._classify_status(resp.status_code, body, resp.headers)
                event = "message"
                data_lines: list[str] = []
                try:
                    async for line in resp.aiter_lines():
                        if line == "":
                            if data_lines:
                                yield event, "\n".join(data_lines), resp.headers
                            event, data_lines = "message", []
                            continue
                        if line.startswith(":"):
                            continue
                        field_, _, value = line.partition(":")
                        value = value[1:] if value.startswith(" ") else value
                        if field_ == "event":
                            event = value
                        elif field_ == "data":
                            data_lines.append(value)
                    if data_lines:
                        yield event, "\n".join(data_lines), resp.headers
                except httpx.HTTPError as exc:
                    raise self._classify_exception(exc, after_send=True) from exc
        except httpx.HTTPError as exc:
            raise self._classify_exception(exc, after_send=False) from exc


def _extract_message(body: str) -> str | None:
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return body[:200] if body else None
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err.get("type") or "")[:500]
    if isinstance(err, str):
        return err[:500]
    if isinstance(data, dict) and "message" in data:
        return str(data["message"])[:500]
    return None
