"""OpenAI-compatible chat backend (DeepSeek, OpenRouter, local gateways).

Configuration comes from the environment so no key is ever committed:

- ``FORGE_OPENAI_BASE_URL``   e.g. ``https://api.deepseek.com/v1``
- ``FORGE_OPENAI_MODEL``      e.g. ``deepseek-chat``
- ``FORGE_OPENAI_API_KEY``    sent as a bearer token when set
- ``FORGE_INPUT_COST_PER_MTOK`` / ``FORGE_OUTPUT_COST_PER_MTOK`` optional rates

No live endpoint is used by the test suite; all tests use an httpx
``MockTransport``.
"""

import os

import httpx

from forge.model.base import (
    Message,
    ModelBackendError,
    ModelUsage,
    cost_from_tokens,
    usage_from_env,
)

DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAICompatibleBackend:
    """Calls POST ``/chat/completions`` and records token usage/cost."""

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_s: float = 300.0,
        transport: httpx.BaseTransport | None = None,
        input_cost_per_mtok: float | None = None,
        output_cost_per_mtok: float | None = None,
    ) -> None:
        self.model = model or os.environ.get("FORGE_OPENAI_MODEL", "")
        if not self.model:
            raise ValueError("FORGE_OPENAI_MODEL (or model=) is required")
        base = base_url or os.environ.get("FORGE_OPENAI_BASE_URL", DEFAULT_BASE_URL)
        self.base_url = base.rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get(
            "FORGE_OPENAI_API_KEY", ""
        )
        self.timeout_s = timeout_s
        env_in, env_out = usage_from_env()
        self.input_cost_per_mtok = (env_in if input_cost_per_mtok is None
                                    else input_cost_per_mtok)
        self.output_cost_per_mtok = (env_out if output_cost_per_mtok is None
                                     else output_cost_per_mtok)
        self.last_usage = ModelUsage()
        self._client = httpx.Client(
            base_url=self.base_url, transport=transport, timeout=timeout_s
        )

    @classmethod
    def from_env(cls, transport: httpx.BaseTransport | None = None) -> "OpenAICompatibleBackend":
        return cls(transport=transport)

    def chat(self, messages: list[Message], json_mode: bool = False) -> str:
        payload: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = self._client.post("/chat/completions", json=payload,
                                         headers=headers)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ModelBackendError(f"OpenAI-compatible request failed: {exc}") from exc
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelBackendError(
                f"unexpected OpenAI-compatible response shape: {exc}"
            ) from exc
        if not isinstance(content, str):
            raise ModelBackendError("OpenAI-compatible message content is not a string")
        usage = body.get("usage", {}) if isinstance(body, dict) else {}
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        self.last_usage = ModelUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_from_tokens(
                prompt_tokens, completion_tokens,
                self.input_cost_per_mtok, self.output_cost_per_mtok,
            ),
        )
        return content
