"""Ollama chat backend over the local HTTP API (sequential use only)."""

import httpx

from forge.model.base import Message, ModelBackendError, ModelUsage

DEFAULT_BASE_URL = "http://127.0.0.1:11434"


class OllamaBackend:
    """Calls POST /api/chat on a local Ollama server. Non-streaming.

    ``json_mode=True`` sets Ollama's ``format: "json"`` so tool-call turns are
    constrained to JSON. Sequential by policy: nothing here parallelises, and
    the runtime never issues overlapping requests.
    """

    def __init__(
        self,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 300.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self.timeout_s = timeout_s
        self.last_usage = ModelUsage()
        # base_url is always set so relative paths like "/api/chat" resolve,
        # including with a MockTransport in tests.
        self._client = httpx.Client(base_url=base_url, transport=transport, timeout=timeout_s)

    def chat(self, messages: list[Message], json_mode: bool = False) -> str:
        payload: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        if json_mode:
            payload["format"] = "json"
        try:
            response = self._client.post("/api/chat", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ModelBackendError(f"Ollama request failed: {exc}") from exc
        try:
            body = response.json()
            content = body["message"]["content"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelBackendError(f"unexpected Ollama response shape: {exc}") from exc
        if not isinstance(content, str):
            raise ModelBackendError("Ollama message content is not a string")
        prompt_tokens = int(body.get("prompt_eval_count", 0) or 0)
        completion_tokens = int(body.get("eval_count", 0) or 0)
        self.last_usage = ModelUsage(
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            cost_usd=0.0,  # local model: no per-token charge
        )
        return content
