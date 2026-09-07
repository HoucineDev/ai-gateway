"""OpenAI (hosted) adapter. Same wire format as openai_compat with hosted-specific defaults."""

from __future__ import annotations

from aigw.adapters.base import Capabilities, DeploymentConfig
from aigw.adapters.openai_compat import _COMMON_PARAMS, OpenAICompatAdapter


class OpenAIAdapter(OpenAICompatAdapter):
    provider = "openai"
    default_base_url = "https://api.openai.com/v1"
    max_tokens_field = "max_completion_tokens"
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
        provider_extensions_passthrough=False,
        supported_params=set(_COMMON_PARAMS),
    )

    def _headers(self, deployment: DeploymentConfig, credential: str) -> dict[str, str]:
        h = super()._headers(deployment, credential)
        org = (deployment.capabilities or {}).get("openai_organization")
        if org:
            h["openai-organization"] = org
        return h
