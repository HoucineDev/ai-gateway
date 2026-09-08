from __future__ import annotations

import httpx

from aigw.adapters.anthropic import AnthropicAdapter
from aigw.adapters.azure_openai import AzureOpenAIAdapter
from aigw.adapters.base import BaseHTTPAdapter
from aigw.adapters.gemini import GeminiAdapter
from aigw.adapters.openai import OpenAIAdapter
from aigw.adapters.openai_compat import OpenAICompatAdapter
from aigw.core.errors import ErrorType, GatewayError

PROVIDERS = ("openai_compat", "openai", "anthropic")


class AdapterRegistry:
    def __init__(self, client: httpx.AsyncClient, default_timeout: float = 120.0, connect_timeout: float = 10.0):
        kw = dict(default_timeout=default_timeout, connect_timeout=connect_timeout)
        self._adapters: dict[str, BaseHTTPAdapter] = {
            "openai_compat": OpenAICompatAdapter(client, **kw),
            "openai": OpenAIAdapter(client, **kw),
            "anthropic": AnthropicAdapter(client, **kw),
            "azure_openai": AzureOpenAIAdapter(client, **kw),
            "gemini": GeminiAdapter(client, **kw),
        }

    def get(self, provider: str) -> BaseHTTPAdapter:
        try:
            return self._adapters[provider]
        except KeyError:
            raise GatewayError(
                ErrorType.unavailable, f"no adapter for provider '{provider}'", code="no_adapter"
            ) from None
