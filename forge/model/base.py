"""Model backend protocol and shared errors."""

from typing import Any, Protocol

Message = dict[str, str]  # {"role": "system"|"user"|"assistant", "content": str}


class ModelBackendError(Exception):
    """The model backend failed (unreachable, HTTP error, malformed response)."""


class ModelBackend(Protocol):
    """A chat backend. Implementations must be safe for sequential use only."""

    def chat(self, messages: list[Message], json_mode: bool = False) -> str:
        """Return the assistant's next message content."""
        ...  # pragma: no cover


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
