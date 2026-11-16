"""Model backend protocol and shared usage/cost types."""

from dataclasses import dataclass
from typing import Any, Protocol

Message = dict[str, str]  # {"role": "system"|"user"|"assistant", "content": str}


class ModelBackendError(Exception):
    """The model backend failed (unreachable, HTTP error, malformed response)."""


@dataclass(frozen=True)
class ModelUsage:
    """Token and cost accounting for one model call (or an accumulated run)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: "ModelUsage") -> "ModelUsage":
        return ModelUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )


class ModelBackend(Protocol):
    """A chat backend. Implementations must be safe for sequential use only."""

    last_usage: ModelUsage

    def chat(self, messages: list[Message], json_mode: bool = False) -> str:
        """Return the assistant's next message content."""
        ...  # pragma: no cover


def usage_from_env(
    default_prompt: float = 0.0, default_completion: float = 0.0
) -> tuple[float, float]:
    """Input/output USD-per-million-token rates from the environment."""
    import os

    def _rate(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, str(default)))
        except ValueError:
            return default

    return (
        _rate("FORGE_INPUT_COST_PER_MTOK", default_prompt),
        _rate("FORGE_OUTPUT_COST_PER_MTOK", default_completion),
    )


def cost_from_tokens(prompt_tokens: int, completion_tokens: int,
                     input_per_mtok: float, output_per_mtok: float) -> float:
    return (prompt_tokens * input_per_mtok + completion_tokens * output_per_mtok) / 1_000_000


def last_user_content(messages: list[Message]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content", ""))
    return ""


def system_content(messages: list[Message]) -> Any:
    for message in messages:
        if message.get("role") == "system":
            return message.get("content", "")
    return ""
