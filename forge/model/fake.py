"""Deterministic scripted model for tests and offline dry-runs."""

from forge.model.base import Message, ModelBackend


class FakeModel(ModelBackend):
    """Replays a fixed script of raw responses, one per chat() call.

    Script entries are raw strings exactly as a backend would return them —
    including deliberately malformed JSON when testing recovery paths. An
    exhausted script is a test-authoring bug and raises immediately.
    """

    def __init__(self, script: list[str]) -> None:
        self.script = list(script)
        self.calls: list[tuple[int, bool]] = []

    def chat(self, messages: list[Message], json_mode: bool = False) -> str:
        self.calls.append((len(messages), json_mode))
        if not self.script:
            raise RuntimeError("FakeModel script exhausted")
        return self.script.pop(0)
