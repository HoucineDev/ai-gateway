"""Azure OpenAI adapter (docs/spec/03 §4.1).

Same wire format as ``openai`` with Azure's addressing and authentication:

* URL ``{base_url}/openai/deployments/{provider_model}/{chat/completions|embeddings}?api-version=…`` — ``base_url``
  is the resource endpoint (``https://<resource>.openai.azure.com``) and ``provider_model`` is the *deployment name*
  in Azure, not the model family;
* ``api-key`` header (``capabilities.auth = "api_key"``, default) or an Entra ID bearer token
  (``capabilities.auth = "bearer"``) resolved from the same ``credential_ref``;
* ``api-version`` from ``capabilities.api_version`` (default ``2024-10-21``);
* the ``model`` field is omitted from the body (the deployment in the path selects it).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from aigw.adapters.base import Capabilities, DeploymentConfig
from aigw.adapters.openai_compat import _COMMON_PARAMS, OpenAICompatAdapter
from aigw.core.errors import ErrorType, GatewayError
from aigw.core.types import ChatRequest

DEFAULT_API_VERSION = "2024-10-21"


class AzureOpenAIAdapter(OpenAICompatAdapter):
    provider = "azure_openai"
    default_base_url = ""  # no sensible default: every Azure resource has its own endpoint
    max_tokens_field = "max_tokens"  # GA api-versions accept it for every deployment; o-series can override
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

    @staticmethod
    def api_version(deployment: DeploymentConfig) -> str:
        return str((deployment.capabilities or {}).get("api_version") or DEFAULT_API_VERSION)

    @staticmethod
    def resource_base(deployment: DeploymentConfig) -> str:
        base = (deployment.base_url or "").rstrip("/")
        if not base:
            raise GatewayError(
                ErrorType.unavailable,
                f"azure_openai deployment '{deployment.name}' has no base_url (resource endpoint)",
                code="azure_base_url_required",
            )
        if base.endswith("/openai"):
            base = base[: -len("/openai")]
        return base

    def _url(self, deployment: DeploymentConfig, path: str) -> str:
        base = self.resource_base(deployment)
        dep = quote(deployment.provider_model, safe="")
        return f"{base}/openai/deployments/{dep}{path}?api-version={self.api_version(deployment)}"

    def _headers(self, deployment: DeploymentConfig, credential: str) -> dict[str, str]:
        h = {"content-type": "application/json", **(deployment.extra_headers or {})}
        mode = str((deployment.capabilities or {}).get("auth") or "api_key")
        if credential:
            if mode == "bearer":
                h["authorization"] = f"Bearer {credential}"
            else:
                h["api-key"] = credential
        return h

    def _payload(self, req: ChatRequest, deployment: DeploymentConfig) -> dict[str, Any]:
        body = super()._payload(req, deployment)
        body.pop("model", None)  # selected by the deployment in the URL
        field = (deployment.capabilities or {}).get("max_tokens_field")
        if field and field != self.max_tokens_field and self.max_tokens_field in body:
            body[str(field)] = body.pop(self.max_tokens_field)
        return body

    def probe_request(self, deployment: DeploymentConfig, credential: str) -> tuple[str, dict[str, str]]:
        """Active health check target (docs/spec/04 §6): the resource's model list."""
        return (
            f"{self.resource_base(deployment)}/openai/models?api-version={self.api_version(deployment)}",
            self._headers(deployment, credential),
        )
