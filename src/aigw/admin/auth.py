"""Control API authentication: admin key (alpha) or OIDC/Keycloak JWT (optional, docs/spec/07 P2 preview)."""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from fastapi import Header, Request

from aigw.core.errors import ErrorType, GatewayError


@dataclass
class Actor:
    type: str  # admin_key | user
    id: str


async def require_admin(
    request: Request, x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)
) -> Actor:
    settings = request.app.state.settings
    if settings.admin_key and x_admin_key and secrets.compare_digest(x_admin_key, settings.admin_key):
        return Actor("admin_key", "admin")
    if settings.oidc_issuer and authorization and authorization.lower().startswith("bearer "):
        return _verify_jwt(authorization[7:], settings)
    if not settings.admin_key and not settings.oidc_issuer:
        raise GatewayError(
            ErrorType.unavailable,
            "Control API has no credential configured (AIGW_ADMIN_KEY)",
            code="admin_not_configured",
        )
    raise GatewayError(ErrorType.authentication, "Admin credentials required", code="admin_auth_required")


_jwks_cache: dict = {}


def _verify_jwt(token: str, settings) -> Actor:
    import jwt

    try:
        client = _jwks_cache.get(settings.oidc_issuer)
        if client is None:
            client = jwt.PyJWKClient(f"{settings.oidc_issuer.rstrip('/')}/protocol/openid-connect/certs")
            _jwks_cache[settings.oidc_issuer] = client
        key = client.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256", "ES256"],
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
            options={"require": ["exp", "iss", "sub"]},
        )
    except Exception as exc:
        raise GatewayError(ErrorType.authentication, f"Invalid token: {exc}", code="invalid_token") from None
    roles = set((claims.get("realm_access") or {}).get("roles") or [])
    for res in (claims.get("resource_access") or {}).values():
        roles |= set(res.get("roles") or [])
    if settings.oidc_admin_role not in roles:
        raise GatewayError(ErrorType.permission, "Missing admin role", code="missing_role")
    return Actor("user", claims.get("preferred_username") or claims["sub"])
