"""Virtual key authentication (docs/spec/04 §2)."""

from __future__ import annotations

import hashlib
import secrets
import time

from aigw.core.errors import ErrorType, GatewayError
from aigw.gateway.snapshot import KeyScope, SnapshotStore

KEY_PREFIX = "aigw_"


def generate_key() -> tuple[str, str, str]:
    """Return (plaintext, sha256 hash, display prefix)."""
    plaintext = KEY_PREFIX + secrets.token_urlsafe(30)[:40]
    return plaintext, hash_key(plaintext), plaintext[:12]


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def authenticate(authorization: str | None, store: SnapshotStore) -> KeyScope:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise GatewayError(ErrorType.authentication, "Missing bearer token", code="missing_key")
    token = authorization[7:].strip()
    if not token.startswith(KEY_PREFIX):
        raise GatewayError(ErrorType.authentication, "Invalid API key", code="invalid_key")
    if store.is_stale():
        raise GatewayError(
            ErrorType.unavailable, "Gateway configuration is stale; refusing to authenticate", code="config_stale"
        )
    scope = store.lookup_key(hash_key(token))
    if scope is None:
        raise GatewayError(ErrorType.authentication, "Invalid API key", code="invalid_key")
    now = time.time()
    if scope.status == "revoked":
        if scope.grace_until and scope.grace_until > now:
            return scope  # rotated key inside its grace period
        raise GatewayError(ErrorType.authentication, "API key has been revoked", code="key_revoked")
    if scope.status != "active":
        raise GatewayError(ErrorType.authentication, "API key is not active", code="key_inactive")
    if scope.expires_at and scope.expires_at < now:
        raise GatewayError(ErrorType.authentication, "API key has expired", code="key_expired")
    return scope
