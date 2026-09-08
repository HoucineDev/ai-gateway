"""Exact response cache (docs/spec/04 §9): opt-in per project, tenant-scoped, Valkey-backed, never on the money path.

A cache entry is keyed by org, project, logical model, the chosen deployment's provider and provider model, and a
hash of the canonical request body (docs/spec/02 §"Caching"), so two projects never share an answer and two
providers never serve each other's. Hits record a `cached` attempt with zero cost; misses go through the normal
reserve → upstream → settle path and store the settled response. Valkey down means every lookup is a miss.

Request header ``X-AIGW-Cache``: ``no-store`` skips read and write, ``no-cache`` skips the read but refreshes the
entry. Response header ``X-AIGW-Cache``: ``hit`` | ``miss`` | ``refresh`` | ``bypass`` | ``off``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import redis.asyncio as redis

from aigw.core.types import ChatRequest, EmbeddingRequest

log = logging.getLogger(__name__)

HEADER = "x-aigw-cache"
DEFAULT_TTL_SECONDS = 300
# request fields that do not change the answer and must not fragment the cache
_EXCLUDED_FIELDS = {"stream", "stream_options", "user", "metadata"}


@dataclass(frozen=True)
class CachePolicy:
    """Per-project policy from `projects.settings.cache` = {"enabled": true, "ttl_seconds": 300,
    "deterministic_only": false}."""

    ttl_seconds: int
    deterministic_only: bool = False

    @classmethod
    def from_project_settings(cls, settings: dict | None) -> CachePolicy | None:
        cfg = (settings or {}).get("cache")
        if not isinstance(cfg, dict) or not cfg.get("enabled"):
            return None
        raw_ttl = cfg.get("ttl_seconds")
        ttl = DEFAULT_TTL_SECONDS if raw_ttl is None else int(raw_ttl)  # explicit 0 disables
        if ttl <= 0:
            return None
        return cls(ttl_seconds=ttl, deterministic_only=bool(cfg.get("deterministic_only", False)))


def is_deterministic(req: ChatRequest | EmbeddingRequest) -> bool:
    """Embeddings always; chat when sampling is pinned (temperature 0 or an explicit seed)."""
    if isinstance(req, EmbeddingRequest):
        return True
    temperature = getattr(req, "temperature", None)
    seed = getattr(req, "seed", None)
    return temperature == 0 or seed is not None


def parse_directive(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower()
    return v if v in ("no-store", "no-cache") else None


def cache_key(scope, model_name: str, deployment, req: ChatRequest | EmbeddingRequest) -> str:
    body = req.model_dump(mode="json", exclude_none=True, exclude=_EXCLUDED_FIELDS)
    material = json.dumps(
        {
            "org": scope.org_id,
            "project": scope.project_id,
            "model": model_name,
            "provider": deployment.provider,
            "provider_model": deployment.provider_model,
            "body": body,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"rc:{scope.project_id}:{hashlib.sha256(material.encode()).hexdigest()}"


class ResponseCache:
    def __init__(self, client: redis.Redis | None, max_entry_bytes: int = 262_144):
        self.client = client
        self.max_entry_bytes = int(max_entry_bytes)

    async def get(self, key: str) -> dict[str, Any] | None:
        if self.client is None:
            return None
        try:
            raw = await self.client.get(key)
        except (redis.RedisError, OSError) as exc:
            log.debug("cache get failed: %s", exc)
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    async def put(self, key: str, entry: dict[str, Any], ttl_seconds: int) -> bool:
        if self.client is None:
            return False
        payload = json.dumps({**entry, "stored_at": time.time()}, separators=(",", ":"))
        if len(payload) > self.max_entry_bytes:
            return False
        try:
            await self.client.set(key, payload, ex=max(int(ttl_seconds), 1))
            return True
        except (redis.RedisError, OSError) as exc:
            log.debug("cache put failed: %s", exc)
            return False
