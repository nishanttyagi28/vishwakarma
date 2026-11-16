"""Deterministic scripted model for tests and offline dry-runs."""

from forge.model.base import Message, ModelBackend, ModelUsage


class FakeModel(ModelBackend):
    """Replays a fixed script of raw responses, one per chat() call.

    Script entries are raw strings exactly as a backend would return them —
    including deliberately malformed JSON when testing recovery paths. An
    exhausted script is a test-authoring bug and raises immediately.
    """

    def __init__(self, script: list[str],
                 usages: list[ModelUsage] | None = None) -> None:
        self.script = list(script)
        self.usages = list(usages or [])
        self.calls: list[tuple[int, bool]] = []
        self.last_usage = ModelUsage()

    def chat(self, messages: list[Message], json_mode: bool = False) -> str:
        index = len(self.calls)
        self.calls.append((len(messages), json_mode))
        self.last_usage = (self.usages[index] if index < len(self.usages)
                           else ModelUsage())
        if not self.script:
            raise RuntimeError("FakeModel script exhausted")
        return self.script.pop(0)
