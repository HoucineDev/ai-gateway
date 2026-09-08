"""Control API authentication and authorization (docs/spec/01 §3.1 global roles, §3.2 delegated roles).

Two credentials are accepted on ``/admin/v1``:

* ``X-Admin-Key`` — the bootstrap credential (``AIGW_ADMIN_KEY``); it carries every scope.
* ``Authorization: Bearer <JWT>`` — an access token issued by the configured OIDC issuer (Keycloak). The
  signature is verified against the issuer's JWKS; ``iss``, ``exp``, ``iat``, ``sub`` and (when configured)
  ``aud`` are checked. Keycloak realm roles (``realm_access.roles``), client roles of the configured client
  (``resource_access.<client>.roles``) and a flat ``roles`` claim are then mapped to *global* scopes through
  ``AIGW_OIDC_ROLE_SCOPES``. Active ``role_bindings`` rows for the token's subject add *delegated* grants that
  hold only inside one organization, team or project. A subject with neither is refused with 403 ``missing_role``.

Scopes are ``<resource>:<read|write>`` (see ``RESOURCES``). ``*``, ``*:read``, ``*:write`` and ``<resource>:*``
are accepted as wildcards. Every control-API route declares the scope it needs through ``require_scope`` (enforced
by ``tests/test_admin_oidc.py``) and then calls ``actor.require(scope, org_id=..., team_id=..., project_id=...)``
with the target's tenant ids so a delegate cannot reach across tenants; the router-level ``require_admin`` is the
fail-closed backstop so an undeclared route still needs a credential.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from dataclasses import dataclass

import httpx
import jwt
from fastapi import Depends, Header, Request

from aigw.core.errors import ErrorType, GatewayError

log = logging.getLogger(__name__)

RESOURCES = (
    "organizations",
    "teams",
    "projects",
    "keys",
    "models",
    "deployments",
    "prices",
    "budgets",
    "usage",
    "requests",
    "audit",
    "config",
    "role_bindings",
)
ACTIONS = ("read", "write")
ALL_SCOPES = tuple(f"{r}:{a}" for r in RESOURCES for a in ACTIONS)

# Delegated roles (docs/spec/01 §3.2): what each role may do *inside the tenant it is bound to*.
DELEGATED_ROLES = ("org_owner", "team_owner", "project_member")
ROLE_RANK = {"org_owner": 3, "team_owner": 2, "project_member": 1}
_ROLE_SCOPES: dict[str, frozenset[str]] = {
    "org_owner": frozenset(
        {
            "organizations:*",
            "teams:*",
            "projects:*",
            "keys:*",
            "models:*",
            "deployments:*",
            "prices:read",
            "budgets:*",
            "usage:read",
            "requests:read",
            "audit:read",
            "config:read",
            "role_bindings:*",
        }
    ),
    "team_owner": frozenset(
        {
            "organizations:read",
            "teams:*",
            "projects:*",
            "keys:*",
            "models:read",
            "deployments:read",
            "prices:read",
            "budgets:*",
            "usage:read",
            "requests:read",
            "config:read",
            "role_bindings:*",
        }
    ),
    "project_member": frozenset(
        {
            "organizations:read",
            "teams:read",
            "projects:read",
            "keys:*",
            "models:read",
            "deployments:read",
            "prices:read",
            "budgets:read",
            "usage:read",
            "requests:read",
            "config:read",
            "role_bindings:read",
        }
    ),
}

# Asymmetric algorithms only: a JWKS never legitimately carries an HMAC secret, and accepting "none"/HS* would let a
# client sign tokens with the public key.
_ALLOWED_ALGS = frozenset({"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"})
_HEADER_TYPES = frozenset({"jwt", "at+jwt"})
_KEYCLOAK_ACCESS_TYP = "bearer"  # Keycloak's `typ` claim: Bearer (access), ID (identity), Refresh


def validate_scope_pattern(pattern: str) -> str:
    """Accept ``*``, ``*:<action>``, ``<resource>:*`` or ``<resource>:<action>``; raise ValueError otherwise."""
    if pattern == "*":
        return pattern
    resource, sep, action = pattern.partition(":")
    ok = bool(sep) and (resource == "*" or resource in RESOURCES) and (action == "*" or action in ACTIONS)
    if not ok or pattern == "*:*":
        raise ValueError(f"invalid scope pattern {pattern!r}; expected <resource>:<read|write> or a wildcard")
    return pattern


def _matches(patterns: frozenset[str], scope: str) -> bool:
    resource, _, action = scope.partition(":")
    return bool(patterns & {"*", scope, f"*:{action}", f"{resource}:*"})


@dataclass(frozen=True)
class Grant:
    """One active role binding, denormalized so covering a target never needs a lookup."""

    role: str
    scope_type: str  # organization | team | project
    org_id: uuid.UUID
    team_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    binding_id: uuid.UUID | None = None

    @property
    def rank(self) -> int:
        return ROLE_RANK[self.role]

    @property
    def patterns(self) -> frozenset[str]:
        return _ROLE_SCOPES[self.role]

    def covers(self, org_id=None, team_id=None, project_id=None) -> bool:
        if self.scope_type == "organization":
            return org_id is not None and org_id == self.org_id
        if self.scope_type == "team":
            return team_id is not None and team_id == self.team_id
        return project_id is not None and project_id == self.project_id


@dataclass(frozen=True)
class Actor:
    type: str  # admin_key | user
    id: str  # display identity used in audit rows (preferred_username, else sub)
    scopes: frozenset[str] = frozenset({"*"})  # global scope patterns
    roles: frozenset[str] = frozenset()
    grants: tuple[Grant, ...] = ()
    subject: str | None = None  # token `sub`
    email: str | None = None

    # ---- global gate ----------------------------------------------------

    def allows(self, scope: str) -> bool:
        """Gate check: some global scope or some grant includes ``scope`` (tenant not yet known)."""
        return _matches(self.scopes, scope) or any(_matches(g.patterns, scope) for g in self.grants)

    def effective_scopes(self) -> list[str]:
        return [s for s in ALL_SCOPES if self.allows(s)]

    # ---- tenant check ---------------------------------------------------

    def unrestricted(self, scope: str) -> bool:
        """True when a global scope covers ``scope``: no tenant predicate applies."""
        return _matches(self.scopes, scope)

    def permits(self, scope: str, *, org_id=None, team_id=None, project_id=None) -> bool:
        if self.unrestricted(scope):
            return True
        return any(_matches(g.patterns, scope) and g.covers(org_id, team_id, project_id) for g in self.grants)

    def require(self, scope: str, *, org_id=None, team_id=None, project_id=None) -> None:
        """Raise 403 unless ``scope`` is permitted on the target tenant (global targets need a global scope)."""
        if not self.permits(scope, org_id=org_id, team_id=team_id, project_id=project_id):
            raise GatewayError(
                ErrorType.permission,
                f"Scope {scope} is not granted on this tenant",
                code="tenant_forbidden",
                details={"required_scope": scope},
            )

    def max_rank(self, *, org_id=None, team_id=None, project_id=None) -> int:
        """Highest delegated rank covering the target; global actors rank above every delegate."""
        if self.unrestricted("role_bindings:write"):
            return max(ROLE_RANK.values()) + 1
        return max((g.rank for g in self.grants if g.covers(org_id, team_id, project_id)), default=0)

    @property
    def identities(self) -> frozenset[str]:
        """Strings a role binding's ``subject`` may equal for this actor."""
        return frozenset(s for s in (self.subject, self.id, self.email) if s)


ADMIN_KEY_ACTOR = Actor("admin_key", "admin")


class OIDCVerifier:
    """Validates Keycloak-issued JWTs against the issuer's JWKS and maps roles to scopes.

    Signing keys are cached for ``oidc_jwks_cache_seconds``. An unknown ``kid`` triggers a refetch (key rotation)
    at most once per ``oidc_jwks_min_refresh_seconds`` so garbage tokens cannot turn the gateway into a JWKS
    hammer. A fetch failure keeps the previous keys; with no keys at all the control API answers 503.
    """

    def __init__(self, settings, http: httpx.AsyncClient | None = None):
        if not settings.oidc_issuer:
            raise ValueError("AIGW_OIDC_ISSUER is required to build an OIDCVerifier")
        self.issuer: str = settings.oidc_issuer.rstrip("/")
        self.audience: str | None = settings.oidc_audience
        self.client_id: str | None = settings.oidc_client_id or settings.oidc_audience
        self.jwks_url: str = settings.oidc_jwks_url or f"{self.issuer}/protocol/openid-connect/certs"
        self.role_scopes: dict[str, frozenset[str]] = {
            role: frozenset(validate_scope_pattern(p) for p in patterns)
            for role, patterns in settings.oidc_role_scopes.items()
        }
        self.leeway = float(settings.oidc_leeway_seconds)
        self.cache_ttl = float(settings.oidc_jwks_cache_seconds)
        self.min_refresh = float(settings.oidc_jwks_min_refresh_seconds)
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(5.0))
        self._owns_http = http is None
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at = float("-inf")  # last successful fetch (monotonic)
        self._attempted_at = float("-inf")  # last fetch attempt, success or not (throttle)
        self._lock = asyncio.Lock()
        self.fetch_count = 0

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ---- token verification -------------------------------------------

    async def verify(self, token: str, *, allow_unmapped: bool = False) -> Actor:
        """Validate ``token`` and build the actor with its global scopes.

        With ``allow_unmapped=False`` (default) a token whose roles map to no scope is refused with 403
        ``missing_role``; ``require_admin`` passes ``True`` because delegated bindings may still grant access.
        """
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise _invalid(f"malformed token: {exc}") from None
        alg = header.get("alg")
        if alg not in _ALLOWED_ALGS:
            raise _invalid(f"unsupported algorithm {alg!r}")
        typ = header.get("typ")
        if typ and str(typ).lower() not in _HEADER_TYPES:
            raise _invalid(f"unsupported token type {typ!r}")
        kid = header.get("kid")
        if not kid:
            raise _invalid("token has no kid header")
        key = await self._signing_key(str(kid))
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=[alg],
                issuer=self.issuer,
                audience=self.audience,
                leeway=self.leeway,
                options={"require": ["exp", "iat", "iss", "sub"], "verify_aud": self.audience is not None},
            )
        except jwt.PyJWTError as exc:
            raise _invalid(str(exc)) from None
        claim_typ = claims.get("typ")
        if claim_typ and str(claim_typ).lower() != _KEYCLOAK_ACCESS_TYP:
            raise _invalid(f"not an access token (typ={claim_typ})")
        roles = self.roles_of(claims)
        scopes: set[str] = set()
        for role in roles:
            scopes |= self.role_scopes.get(role, frozenset())
        if not scopes and not allow_unmapped:
            raise missing_role(roles)
        return Actor(
            "user",
            str(claims.get("preferred_username") or claims["sub"]),
            frozenset(scopes),
            frozenset(roles),
            subject=str(claims["sub"]),
            email=str(claims["email"]) if claims.get("email") else None,
        )

    def roles_of(self, claims: dict) -> set[str]:
        roles = set(_as_str_list(claims.get("roles")))
        roles |= set(_as_str_list((claims.get("realm_access") or {}).get("roles")))
        if self.client_id:
            client = (claims.get("resource_access") or {}).get(self.client_id) or {}
            roles |= set(_as_str_list(client.get("roles")))
        return roles

    # ---- JWKS cache ----------------------------------------------------

    async def _signing_key(self, kid: str) -> jwt.PyJWK:
        now = time.monotonic()
        key = self._keys.get(kid)
        if key is not None and now - self._fetched_at < self.cache_ttl:
            return key
        async with self._lock:
            now = time.monotonic()
            key = self._keys.get(kid)
            if key is not None and now - self._fetched_at < self.cache_ttl:
                return key
            if not self._keys or now - self._attempted_at >= self.min_refresh:
                await self._refresh()
            key = self._keys.get(kid)
        if key is None:
            raise GatewayError(ErrorType.authentication, "Unknown signing key", code="unknown_signing_key")
        return key

    async def _refresh(self) -> None:
        self._attempted_at = time.monotonic()
        self.fetch_count += 1
        try:
            resp = await self._http.get(self.jwks_url)
            resp.raise_for_status()
            keys = _parse_jwks(resp.json())
        except Exception as exc:
            if self._keys:
                log.warning(
                    "JWKS refresh from %s failed, keeping %d cached keys: %s", self.jwks_url, len(self._keys), exc
                )
                return
            log.error("JWKS fetch from %s failed: %s", self.jwks_url, exc)
            raise GatewayError(
                ErrorType.unavailable, "Identity provider signing keys unavailable", code="oidc_unavailable"
            ) from None
        self._keys = keys
        self._fetched_at = time.monotonic()
        log.info("JWKS loaded from %s: %d signing keys", self.jwks_url, len(keys))


def _parse_jwks(data: dict) -> dict[str, jwt.PyJWK]:
    keys: dict[str, jwt.PyJWK] = {}
    for jwk in data.get("keys") or []:
        if jwk.get("use", "sig") != "sig" or (jwk.get("alg") and jwk["alg"] not in _ALLOWED_ALGS):
            continue
        try:
            key = jwt.PyJWK(jwk)
        except jwt.PyJWTError:
            continue
        if key.key_id:
            keys[key.key_id] = key
    return keys


def _as_str_list(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple | set):
        return [str(v) for v in value]
    return []


def _invalid(reason: str) -> GatewayError:
    return GatewayError(ErrorType.authentication, f"Invalid token: {reason}", code="invalid_token")


def missing_role(roles=()) -> GatewayError:
    return GatewayError(
        ErrorType.permission,
        "Token carries no role mapped to a control-API scope and no active role binding",
        code="missing_role",
        details={"roles": sorted(roles)},
    )


# ---- FastAPI dependencies ---------------------------------------------


async def require_admin(
    request: Request, x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)
) -> Actor:
    """Authenticate the caller (admin key or OIDC bearer) and attach delegated grants. Cached per request by
    FastAPI, so stacking it under ``require_scope`` costs one verification and one bindings query."""
    settings = request.app.state.settings
    verifier: OIDCVerifier | None = getattr(request.app.state, "oidc", None)
    if x_admin_key is not None:
        if settings.admin_key and secrets.compare_digest(x_admin_key.encode(), settings.admin_key.encode()):
            return ADMIN_KEY_ACTOR
        raise GatewayError(ErrorType.authentication, "Invalid admin key", code="invalid_admin_key")
    if authorization and authorization[:7].lower() == "bearer ":
        if verifier is None:
            raise GatewayError(
                ErrorType.authentication,
                "Bearer tokens are not accepted: AIGW_OIDC_ISSUER is not configured",
                code="oidc_not_configured",
            )
        actor = await verifier.verify(authorization[7:].strip(), allow_unmapped=True)
        from aigw.admin.tenancy import load_grants  # local import: tenancy imports Actor from this module

        actor = await load_grants(request.app.state.db, actor)
        if not actor.scopes and not actor.grants:
            raise missing_role(actor.roles)
        return actor
    if not settings.admin_key and verifier is None:
        raise GatewayError(
            ErrorType.unavailable,
            "Control API has no credential configured (AIGW_ADMIN_KEY or AIGW_OIDC_ISSUER)",
            code="admin_not_configured",
        )
    raise GatewayError(ErrorType.authentication, "Admin credentials required", code="admin_auth_required")


def require_scope(scope: str):
    """Dependency factory: authenticate and require ``scope`` (``<resource>:<read|write>``) globally or through
    some delegated grant. Routes then narrow to the target tenant with ``actor.require(...)``."""
    if scope not in ALL_SCOPES:
        raise ValueError(f"unknown scope {scope!r}")

    async def dependency(actor: Actor = Depends(require_admin)) -> Actor:
        if not actor.allows(scope):
            raise GatewayError(
                ErrorType.permission,
                f"Scope {scope} required",
                code="insufficient_scope",
                details={"required_scope": scope},
            )
        return actor

    dependency.scope = scope  # type: ignore[attr-defined]  # introspected by the route-coverage test
    dependency.__name__ = f"require_scope[{scope}]"
    return dependency


__all__ = [
    "ACTIONS",
    "ADMIN_KEY_ACTOR",
    "ALL_SCOPES",
    "DELEGATED_ROLES",
    "RESOURCES",
    "ROLE_RANK",
    "Actor",
    "Grant",
    "OIDCVerifier",
    "missing_role",
    "require_admin",
    "require_scope",
    "validate_scope_pattern",
]
