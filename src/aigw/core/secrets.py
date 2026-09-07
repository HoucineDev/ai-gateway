"""Credential reference resolution (docs/spec/03 §5). Resolved values are never logged or persisted."""

from __future__ import annotations

import os

from aigw.core.errors import ErrorType, GatewayError


class SecretResolver:
    def __init__(self, env: dict[str, str] | None = None):
        self._env = env if env is not None else os.environ

    def resolve(self, ref: str | None) -> str:
        if not ref or ref == "none":
            return ""
        scheme, _, rest = ref.partition(":")
        if scheme == "env":
            value = self._env.get(rest)
            if value is None:
                raise GatewayError(
                    ErrorType.unavailable, f"credential env var '{rest}' is not set", code="credential_missing"
                )
            return value
        if scheme == "literal":  # tests / local development only
            return rest
        if scheme == "openbao":
            raise GatewayError(
                ErrorType.unavailable, "OpenBao secret backend is not enabled (Phase 2)", code="credential_backend"
            )
        raise GatewayError(
            ErrorType.unavailable, f"unknown credential reference scheme '{scheme}'", code="credential_ref"
        )
