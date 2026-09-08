"""Request pipeline (docs/spec/04 §1): authenticate → scope → validate → rate limit → reserve → route →
adapter → settle. One RequestContext carries state through every stage so the order is explicit and testable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from aigw.adapters.base import DeploymentConfig
from aigw.adapters.registry import AdapterRegistry
from aigw.config import Settings
from aigw.core.errors import ErrorClass, ErrorType, GatewayError, UpstreamError
from aigw.core.ids import uuid7
from aigw.core.secrets import SecretResolver
from aigw.core.tokens import estimate_text_tokens
from aigw.core.types import (
    ChatRequest,
    ChatResponse,
    DeltaEvent,
    EmbeddingRequest,
    EndEvent,
    FinishEvent,
    StartEvent,
    ToolCallDelta,
    Usage,
    UsageEvent,
)
from aigw.gateway import metrics
from aigw.gateway.accounting import Ledger, Reservation
from aigw.gateway.cache import ResponseCache, cache_key, is_deterministic, parse_directive
from aigw.gateway.ratelimit import CooldownStore, LimitScope, RateLimiter
from aigw.gateway.router import Candidate, Router, RoutingDecision, RoutingPolicy
from aigw.gateway.signals import RoutingSignals
from aigw.gateway.snapshot import KeyScope, ModelInfo, SnapshotStore

log = logging.getLogger(__name__)


@dataclass
class RequestContext:
    request_id: uuid.UUID
    scope: KeyScope
    model: ModelInfo
    endpoint: str
    req: ChatRequest | EmbeddingRequest
    est_prompt_tokens: int
    est_output_tokens: int
    tags: dict[str, str]
    started: float = field(default_factory=time.time)
    attempt_no: int = 0
    decision: RoutingDecision | None = None
    limit_scopes: list[LimitScope] = field(default_factory=list)
    cache_key: str | None = None  # docs/spec/04 §9
    cache_status: str = "off"  # off | bypass | miss | refresh | hit
    cache_key: str | None = None  # docs/spec/04 §9
    cache_status: str = "off"  # off | bypass | miss | refresh | hit


class Pipeline:
    def __init__(
        self,
        *,
        settings: Settings,
        snapshots: SnapshotStore,
        adapters: AdapterRegistry,
        ledger: Ledger,
        limiter: RateLimiter,
        cooldowns: CooldownStore,
        secrets: SecretResolver,
        router: Router | None = None,
        signals: RoutingSignals | None = None,
        cache: ResponseCache | None = None,
    ):
        self.settings = settings
        self.snapshots = snapshots
        self.adapters = adapters
        self.ledger = ledger
        self.limiter = limiter
        self.cooldowns = cooldowns
        self.secrets = secrets
        self.signals = signals or RoutingSignals.build(None, settings.routing_ewma_alpha)
        self.cache = cache or ResponseCache(None, settings.cache_max_entry_bytes)
        self.cache = cache or ResponseCache(None, settings.cache_max_entry_bytes)
        self.router = router or Router(
            adapters, cooldowns, signals=self.signals, policy=RoutingPolicy.from_settings(settings)
        )
        self._background: set[asyncio.Task] = set()

    def _detach(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ---- preparation -------------------------------------------------------
    async def prepare(
        self,
        scope: KeyScope,
        req: ChatRequest | EmbeddingRequest,
        endpoint: str,
        cache_directive: str | None = None,
    ) -> RequestContext:
        snap = self.snapshots.current
        if snap is None:
            raise GatewayError(ErrorType.unavailable, "Gateway has no configuration loaded", code="config_unavailable")
        model = snap.resolve_model(scope.org_id, req.model)
        if model is None or (scope.allowed_models is not None and model.name not in scope.allowed_models):
            raise GatewayError(
                ErrorType.not_found,
                f"Model '{req.model}' does not exist or you do not have access",
                code="model_not_found",
                param="model",
            )
        if endpoint == "embeddings" and "embedding" not in model.modalities:
            raise GatewayError(
                ErrorType.invalid_request, f"Model '{req.model}' is not an embedding model", param="model"
            )
        if endpoint == "chat" and "chat" not in model.modalities:
            raise GatewayError(ErrorType.invalid_request, f"Model '{req.model}' is not a chat model", param="model")
        tags = self._validate_tags(scope, req.metadata)
        any_adapter = self.adapters.get(model.deployments[0].provider) if model.deployments else None
        est_prompt = any_adapter.estimate_prompt_tokens(req) if any_adapter else 0
        if isinstance(req, ChatRequest):
            est_out = req.max_tokens or self.settings.default_max_output_tokens
        else:
            est_out = 0
        ctx = RequestContext(
            request_id=uuid7(),
            scope=scope,
            model=model,
            endpoint=endpoint,
            req=req,
            est_prompt_tokens=est_prompt,
            est_output_tokens=est_out,
            tags=tags,
        )
        ctx.limit_scopes = [
            LimitScope("key", scope.key_id, scope.rpm_limit, scope.tpm_limit),
            LimitScope("project", scope.project_id, scope.project_rpm_limit, scope.project_tpm_limit),
        ]
        await self.limiter.check(ctx.limit_scopes, est_prompt + est_out)
        ctx.decision = await self.router.route(model, req, endpoint, est_prompt)
        self._plan_cache(ctx, cache_directive)
        return ctx

    def _validate_tags(self, scope: KeyScope, metadata: dict[str, str] | None) -> dict[str, str]:
        if not metadata:
            return {}
        if scope.allowed_tags is not None:
            bad = [k for k in metadata if k not in scope.allowed_tags]
            if bad:
                raise GatewayError(
                    ErrorType.invalid_request,
                    f"Unknown allocation tag(s): {', '.join(bad)}",
                    code="invalid_tag",
                    param="metadata",
                )
        return {k: str(v)[:200] for k, v in list(metadata.items())[:20]}

    # ---- attempt bookkeeping ------------------------------------------------
    async def _reserve(self, ctx: RequestContext, cand: Candidate) -> Reservation:
        self.signals.inflight.inc(cand.deployment.id)
        ctx.attempt_no += 1
        if ctx.attempt_no > self.settings.max_attempts:
            raise GatewayError(ErrorType.unavailable, "Maximum attempts exhausted", code="max_attempts")
        snap = self.snapshots.current
        price = snap.price_for(cand.deployment.provider, cand.deployment.provider_model)
        routing = {**ctx.decision.explain(), "chosen": cand.deployment.name, "attempt": ctx.attempt_no}
        return await self.ledger.reserve(
            scope=ctx.scope,
            request_id=ctx.request_id,
            attempt_no=ctx.attempt_no,
            model_id=ctx.model.id,
            model_name=ctx.model.name,
            deployment=cand.deployment,
            endpoint=ctx.endpoint,
            price=price,
            est_prompt_tokens=ctx.est_prompt_tokens,
            est_output_tokens=ctx.est_output_tokens,
            routing=routing,
        )

    async def _settle(
        self,
        ctx: RequestContext,
        res: Reservation,
        cand: Candidate,
        *,
        status: str,
        usage: Usage | None,
        usage_source: str,
        ttfb: float | None,
        upstream_request_id=None,
        err: UpstreamError | None = None,
        first_byte_at: datetime | None = None,
    ) -> None:
        self.signals.inflight.dec(cand.deployment.id)
        if status == "succeeded" and ttfb is not None:
            self.signals.latency.observe(cand.deployment.id, ttfb)
        latency_ms = int((time.time() - ctx.started) * 1000)
        cost = await self.ledger.settle(
            res,
            scope=ctx.scope,
            status=status,
            usage=usage,
            usage_source=usage_source,
            deployment=cand.deployment,
            model_name=ctx.model.name,
            endpoint=ctx.endpoint,
            stream=getattr(ctx.req, "stream", False),
            latency_ms=latency_ms,
            ttfb_ms=int(ttfb * 1000) if ttfb is not None else None,
            upstream_request_id=upstream_request_id,
            error_class=err.error_class.value if err else None,
            error_message=err.message if err else None,
            tags=ctx.tags,
            first_byte_at=first_byte_at,
        )
        if usage:
            actual_tokens = usage.prompt_tokens + usage.completion_tokens
            await self.limiter.adjust_tokens(
                ctx.limit_scopes, actual_tokens - (ctx.est_prompt_tokens + ctx.est_output_tokens)
            )
        metrics.observe(ctx, cand.deployment, status, usage, cost, latency_ms, ttfb)
        log.info(
            "request %s attempt=%d model=%s deployment=%s status=%s tokens=%s cost=%s latency_ms=%d",
            ctx.request_id,
            ctx.attempt_no,
            ctx.model.name,
            cand.deployment.name,
            status,
            (usage.prompt_tokens, usage.completion_tokens) if usage else None,
            cost,
            latency_ms,
        )

    async def _on_upstream_error(self, cand: Candidate, err: UpstreamError) -> None:
        d = cand.deployment
        secs = err.cooldown_seconds
        if secs is None and err.error_class in (ErrorClass.timeout_before_send, ErrorClass.upstream_error):
            n = self.cooldowns.record_failure(d.id)
            if n >= 3:
                secs = 5.0 if err.error_class == ErrorClass.timeout_before_send else 30.0
        if secs:
            await self.cooldowns.set(d.id, secs)
            metrics.COOLDOWNS.labels(deployment=d.name, reason=err.error_class.value).inc()

    def _credential(self, d: DeploymentConfig) -> str:
        return self.secrets.resolve(d.credential_ref)

    # ---- exact response cache (docs/spec/04 §9) ---------------------------
    def _plan_cache(self, ctx: RequestContext, directive: str | None) -> None:
        scope = ctx.scope
        if scope.cache_ttl_seconds <= 0 or (scope.cache_deterministic_only and not is_deterministic(ctx.req)):
            ctx.cache_status = "off"
            return
        directive = parse_directive(directive)
        if directive == "no-store":
            ctx.cache_status = "bypass"
            return
        first = ctx.decision.ordered[0].deployment
        ctx.cache_key = cache_key(scope, ctx.model.name, first, ctx.req)
        ctx.cache_status = "refresh" if directive == "no-cache" else "miss"

    async def cached(self, ctx: RequestContext) -> dict[str, Any] | None:
        """Serve a hit: records a zero-cost `cached` attempt and returns the wire response, else None."""
        if ctx.cache_status != "miss" or not ctx.cache_key:
            metrics.CACHE.labels(result=ctx.cache_status).inc()
            return None
        entry = await self.cache.get(ctx.cache_key)
        if not entry:
            metrics.CACHE.labels(result="miss").inc()
            return None
        ctx.cache_status = "hit"
        metrics.CACHE.labels(result="hit").inc()
        cand = ctx.decision.ordered[0]
        usage = Usage(**entry.get("usage", {}))
        latency_ms = int((time.time() - ctx.started) * 1000)
        await self.ledger.record_cache_hit(
            request_id=ctx.request_id,
            scope=ctx.scope,
            model_id=ctx.model.id,
            model_name=ctx.model.name,
            deployment=cand.deployment,
            endpoint=ctx.endpoint,
            usage=usage,
            stream=bool(getattr(ctx.req, "stream", False)),
            tags=ctx.tags,
            routing={**ctx.decision.explain(), "chosen": cand.deployment.name, "attempt": 1, "cache": "hit"},
            latency_ms=latency_ms,
        )
        metrics.observe(ctx, cand.deployment, "cached", usage, Decimal(0), latency_ms, None)
        wire = dict(entry["response"])
        wire["id"] = f"chatcmpl-{ctx.request_id}" if ctx.endpoint == "chat" else wire.get("id")
        wire["aigw"] = {
            "request_id": str(ctx.request_id),
            "deployment": cand.deployment.name,
            "provider": cand.deployment.provider,
            "cached": True,
        }
        return wire

    async def _cache_store(
        self, ctx: RequestContext, cand: Candidate, wire: dict[str, Any], usage: Usage | None
    ) -> None:
        if ctx.cache_status not in ("miss", "refresh") or not ctx.cache_key or usage is None:
            return
        entry = {
            "response": {k: v for k, v in wire.items() if k != "aigw"},
            "usage": usage.model_dump(),
            "deployment": cand.deployment.name,
            "provider": cand.deployment.provider,
        }
        await self.cache.put(ctx.cache_key, entry, ctx.scope.cache_ttl_seconds)

    async def replay_stream(self, ctx: RequestContext, wire: dict[str, Any]) -> AsyncIterator[bytes]:
        """Serve a cached chat completion to a streaming client as role → content → finish (→ usage) → [DONE]."""
        req: ChatRequest = ctx.req  # type: ignore[assignment]
        cid, created, model = wire["id"], int(time.time()), ctx.model.name
        for choice in wire.get("choices", []):
            idx = choice.get("index", 0)
            msg = choice.get("message", {})
            yield _sse(_delta_chunk(cid, created, model, DeltaEvent(index=idx, role="assistant")))
            if msg.get("content"):
                yield _sse(_delta_chunk(cid, created, model, DeltaEvent(index=idx, content=msg["content"])))
            if msg.get("tool_calls"):
                calls = [
                    ToolCallDelta(index=i, **{k: v for k, v in tc.items() if k in ("id", "type", "function")})
                    for i, tc in enumerate(msg["tool_calls"])
                ]
                yield _sse(_delta_chunk(cid, created, model, DeltaEvent(index=idx, tool_calls=calls)))
            yield _sse(
                _finish_chunk(
                    cid, created, model, FinishEvent(index=idx, finish_reason=choice.get("finish_reason") or "stop")
                )
            )
        if req.stream_options and req.stream_options.include_usage and wire.get("usage"):
            yield _sse(
                {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [],
                    "usage": wire["usage"],
                }
            )
        yield b"data: [DONE]\n\n"

    @staticmethod
    def _stream_wire(
        ctx: RequestContext, text: str, finish_reason: str | None, usage: Usage, estimated: bool
    ) -> dict[str, Any]:
        return {
            "id": f"chatcmpl-{ctx.request_id}",
            "object": "chat.completion",
            "created": int(ctx.started),
            "model": ctx.model.name,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": finish_reason or "stop",
                }
            ],
            "usage": usage.to_wire(estimated=estimated),
        }

    # ---- unary chat / embeddings ---------------------------------------------
    async def run_unary(self, ctx: RequestContext) -> dict[str, Any]:
        last_err: UpstreamError | None = None
        for cand in ctx.decision.ordered:
            retried = False
            while True:
                res = await self._reserve(ctx, cand)
                adapter = self.adapters.get(cand.deployment.provider)
                t0 = time.time()
                try:
                    if isinstance(ctx.req, ChatRequest):
                        out = await adapter.chat(ctx.req, cand.deployment, self._credential(cand.deployment))
                    else:
                        out = await adapter.embeddings(ctx.req, cand.deployment, self._credential(cand.deployment))
                except UpstreamError as err:
                    last_err = err
                    await self._on_upstream_error(cand, err)
                    ambiguous = err.error_class == ErrorClass.ambiguous
                    await self._settle(
                        ctx,
                        res,
                        cand,
                        status="ambiguous" if ambiguous else "failed",
                        usage=None,
                        usage_source="none",
                        ttfb=None,
                        upstream_request_id=err.upstream_request_id,
                        err=err,
                    )
                    if ambiguous or not err.fallback_allowed and not err.retry_same:
                        raise err.to_gateway_error() from err
                    if err.retry_same and not retried and ctx.attempt_no < self.settings.max_attempts:
                        retried = True
                        continue
                    break  # fallback to next candidate
                except GatewayError:
                    await self._settle(ctx, res, cand, status="failed", usage=None, usage_source="none", ttfb=None)
                    raise
                ttfb = time.time() - t0
                self.cooldowns.record_success(cand.deployment.id)
                usage, source = self._usage_or_estimate(ctx, out.usage, out)
                await self._settle(
                    ctx,
                    res,
                    cand,
                    status="succeeded",
                    usage=usage,
                    usage_source=source,
                    ttfb=ttfb,
                    upstream_request_id=out.upstream_request_id,
                )
                if isinstance(out, ChatResponse):
                    out.usage = usage
                    wire = out.to_wire(
                        id_override=f"chatcmpl-{ctx.request_id}",
                        model_name=ctx.model.name,
                        estimated=source == "estimated",
                    )
                else:
                    out.usage = usage
                    wire = out.to_wire(model_name=ctx.model.name, estimated=source == "estimated")
                wire["aigw"] = {
                    "request_id": str(ctx.request_id),
                    "deployment": cand.deployment.name,
                    "provider": cand.deployment.provider,
                }
                await self._cache_store(ctx, cand, wire, usage)
                return wire
        if last_err:
            raise last_err.to_gateway_error()
        raise GatewayError(
            ErrorType.unavailable, "No deployment could serve the request", code="no_eligible_deployment"
        )

    def _usage_or_estimate(self, ctx: RequestContext, usage: Usage | None, out: Any) -> tuple[Usage, str]:
        if usage and (usage.prompt_tokens or usage.completion_tokens):
            return usage, "reported"
        if isinstance(out, ChatResponse):
            completion = sum(estimate_text_tokens(c.message.text()) for c in out.choices)
        elif isinstance(out, str):
            completion = estimate_text_tokens(out)
        else:
            completion = 0
        return Usage(prompt_tokens=ctx.est_prompt_tokens, completion_tokens=completion), "estimated"

    # ---- streaming chat ------------------------------------------------------
    async def run_stream(self, ctx: RequestContext) -> AsyncIterator[bytes]:
        """Yield SSE bytes. Fallback happens only before the first client-visible byte."""
        req: ChatRequest = ctx.req  # type: ignore[assignment]
        include_usage = bool(req.stream_options and req.stream_options.include_usage)
        chunk_id = f"chatcmpl-{ctx.request_id}"
        created = int(time.time())
        last_err: UpstreamError | None = None

        for cand in ctx.decision.ordered:
            res = await self._reserve(ctx, cand)
            adapter = self.adapters.get(cand.deployment.provider)
            t0 = time.time()
            sent_any = False
            first_byte_at: datetime | None = None
            ttfb: float | None = None
            usage: Usage | None = None
            upstream_rid: str | None = None
            finished = False
            text_len = 0
            parts: list[str] = []  # assembled for the response cache (text-only answers)
            saw_tools = False
            finish_reason = None
            status = "succeeded"
            err_obj: UpstreamError | None = None
            try:
                async for ev in adapter.chat_stream(req, cand.deployment, self._credential(cand.deployment)):
                    if isinstance(ev, StartEvent):
                        upstream_rid = ev.upstream_request_id
                        continue
                    if isinstance(ev, UsageEvent):
                        usage = ev.usage
                        continue
                    if isinstance(ev, EndEvent):
                        break
                    if isinstance(ev, DeltaEvent):
                        if ev.content:
                            text_len += len(ev.content)
                            parts.append(ev.content)
                        if ev.tool_calls:
                            saw_tools = True
                        payload = _delta_chunk(chunk_id, created, ctx.model.name, ev)
                    elif isinstance(ev, FinishEvent):
                        finished = True
                        finish_reason = ev.finish_reason
                        payload = _finish_chunk(chunk_id, created, ctx.model.name, ev)
                    else:
                        continue
                    if not sent_any:
                        sent_any = True
                        ttfb = time.time() - t0
                        first_byte_at = datetime.now(UTC)
                    yield _sse(payload)
            except UpstreamError as err:
                err_obj = err
                await self._on_upstream_error(cand, err)
                if not sent_any and err.fallback_allowed and err.error_class != ErrorClass.ambiguous:
                    last_err = err
                    await self._settle(
                        ctx,
                        res,
                        cand,
                        status="failed",
                        usage=None,
                        usage_source="none",
                        ttfb=None,
                        upstream_request_id=err.upstream_request_id,
                        err=err,
                    )
                    continue  # fallback before first byte
                status = "ambiguous" if (sent_any or err.error_class == ErrorClass.ambiguous) else "failed"
            except asyncio.CancelledError:
                # client went away; settle conservatively and re-raise
                u, src = (
                    (usage, "reported")
                    if usage
                    else (
                        Usage(
                            prompt_tokens=ctx.est_prompt_tokens, completion_tokens=estimate_text_tokens("x" * text_len)
                        ),
                        "estimated",
                    )
                )
                # The request task is being cancelled: run settlement as a detached task so the ledger
                # is finalized even though this coroutine unwinds immediately.
                self._detach(
                    self._settle(
                        ctx,
                        res,
                        cand,
                        status="cancelled",
                        usage=u,
                        usage_source=src,
                        ttfb=ttfb,
                        upstream_request_id=upstream_rid,
                        first_byte_at=first_byte_at,
                    )
                )
                raise
            except GatewayError as gerr:
                await self._settle(ctx, res, cand, status="failed", usage=None, usage_source="none", ttfb=None)
                if not sent_any:
                    raise
                yield _sse({"error": gerr.envelope(str(ctx.request_id))["error"]})
                yield b"data: [DONE]\n\n"
                return

            # settle -----------------------------------------------------------
            if usage and (usage.prompt_tokens or usage.completion_tokens):
                source = "reported"
            else:
                usage = Usage(
                    prompt_tokens=ctx.est_prompt_tokens, completion_tokens=estimate_text_tokens("x" * text_len)
                )
                source = "estimated"
            if status == "succeeded":
                self.cooldowns.record_success(cand.deployment.id)
            await self._settle(
                ctx,
                res,
                cand,
                status=status,
                usage=usage,
                usage_source=source,
                ttfb=ttfb,
                upstream_request_id=upstream_rid,
                err=err_obj,
                first_byte_at=first_byte_at,
            )
            if status == "succeeded" and err_obj is None and finished and not saw_tools and usage is not None:
                await self._cache_store(
                    ctx,
                    cand,
                    self._stream_wire(ctx, "".join(parts), finish_reason, usage, source == "estimated"),
                    usage,
                )
            if err_obj is not None:
                if not sent_any:
                    raise err_obj.to_gateway_error()
                yield _sse({"error": err_obj.to_gateway_error().envelope(str(ctx.request_id))["error"]})
                yield b"data: [DONE]\n\n"
                return
            if not finished:
                yield _sse(_finish_chunk(chunk_id, created, ctx.model.name, FinishEvent(index=0, finish_reason="stop")))
            if include_usage or source == "estimated":
                yield _sse(
                    {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": ctx.model.name,
                        "choices": [],
                        "usage": usage.to_wire(source == "estimated"),
                    }
                )
            yield b"data: [DONE]\n\n"
            return

        if last_err:
            raise last_err.to_gateway_error()
        raise GatewayError(
            ErrorType.unavailable, "No deployment could serve the request", code="no_eligible_deployment"
        )


def _sse(obj: dict) -> bytes:
    return b"data: " + json.dumps(obj, separators=(",", ":")).encode() + b"\n\n"


def _delta_chunk(cid: str, created: int, model: str, ev: DeltaEvent) -> dict:
    delta: dict[str, Any] = {}
    if ev.role:
        delta["role"] = ev.role
    if ev.content is not None:
        delta["content"] = ev.content
    if ev.tool_calls:
        delta["tool_calls"] = [t.model_dump(exclude_none=True) for t in ev.tool_calls]
    return {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": ev.index, "delta": delta, "finish_reason": None}],
    }


def _finish_chunk(cid: str, created: int, model: str, ev: FinishEvent) -> dict:
    return {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": ev.index, "delta": {}, "finish_reason": ev.finish_reason}],
    }
