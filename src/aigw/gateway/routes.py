"""Inference API: /v1/chat/completions, /v1/embeddings, /v1/models (docs/spec/01 §2)."""

from __future__ import annotations

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from aigw.core.errors import ErrorType, GatewayError
from aigw.core.types import ChatRequest, EmbeddingRequest
from aigw.gateway import metrics
from aigw.gateway.auth import authenticate

router = APIRouter(prefix="/v1")


def _validation_error(exc: ValidationError) -> GatewayError:
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(p) for p in first.get("loc", ()))
    msg = first.get("msg", "invalid request")
    if first.get("type") == "extra_forbidden":
        return GatewayError(
            ErrorType.invalid_request, f"Unknown parameter '{loc}'", code="unknown_parameter", param=loc
        )
    return GatewayError(
        ErrorType.invalid_request, f"{loc}: {msg}" if loc else msg, code="validation_error", param=loc or None
    )


async def _parse(request: Request, model_cls):
    try:
        body = await request.json()
    except ValueError:
        raise GatewayError(ErrorType.invalid_request, "Body must be valid JSON", code="invalid_json") from None
    try:
        return model_cls.model_validate(body)
    except ValidationError as exc:
        raise _validation_error(exc) from None


@router.post("/chat/completions")
async def chat_completions(request: Request, authorization: str | None = Header(default=None)):
    st = request.app.state
    scope = authenticate(authorization, st.snapshots)
    req = await _parse(request, ChatRequest)
    try:
        ctx = await st.pipeline.prepare(scope, req, "chat")
    except GatewayError as exc:
        _count_rejection(exc)
        raise
    headers = {"x-aigw-request-id": str(ctx.request_id)}
    if req.stream:
        return StreamingResponse(
            st.pipeline.run_stream(ctx),
            media_type="text/event-stream",
            headers={**headers, "cache-control": "no-cache", "x-accel-buffering": "no"},
        )
    try:
        out = await st.pipeline.run_unary(ctx)
    except GatewayError as exc:
        _count_rejection(exc)
        raise
    headers["x-aigw-deployment"] = out["aigw"]["deployment"]
    headers["x-aigw-provider"] = out["aigw"]["provider"]
    return JSONResponse(out, headers=headers)


@router.post("/embeddings")
async def embeddings(request: Request, authorization: str | None = Header(default=None)):
    st = request.app.state
    scope = authenticate(authorization, st.snapshots)
    req = await _parse(request, EmbeddingRequest)
    try:
        ctx = await st.pipeline.prepare(scope, req, "embeddings")
        out = await st.pipeline.run_unary(ctx)
    except GatewayError as exc:
        _count_rejection(exc)
        raise
    return JSONResponse(
        out,
        headers={
            "x-aigw-request-id": str(ctx.request_id),
            "x-aigw-deployment": out["aigw"]["deployment"],
            "x-aigw-provider": out["aigw"]["provider"],
        },
    )


@router.get("/models")
async def list_models(request: Request, authorization: str | None = Header(default=None)):
    st = request.app.state
    scope = authenticate(authorization, st.snapshots)
    snap = st.snapshots.current
    data = []
    for m in snap.models_for(scope):
        data.append(
            {
                "id": m.name,
                "object": "model",
                "owned_by": "aigw",
                "created": 0,
                "aigw": {
                    "display_name": m.display_name,
                    "modalities": m.modalities,
                    "context_window": m.context_window,
                    "supports_tools": m.supports_tools,
                    "supports_json_schema": m.supports_json_schema,
                    "supports_vision": m.supports_vision,
                    "deployments": len(m.deployments),
                },
            }
        )
    return {"object": "list", "data": data}


def _count_rejection(exc: GatewayError) -> None:
    if exc.type == ErrorType.budget_exceeded:
        metrics.BUDGET_REJECTIONS.labels(scope_type=exc.details.get("scope_type", "unknown")).inc()
    elif exc.type == ErrorType.rate_limit:
        metrics.RATELIMIT_REJECTIONS.labels(kind=exc.code).inc()
