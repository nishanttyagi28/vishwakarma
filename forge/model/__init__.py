"""Model backends: Ollama (live) and FakeModel (deterministic)."""

from forge.model.base import ModelBackend, ModelBackendError
from forge.model.fake import FakeModel
from forge.model.ollama_backend import OllamaBackend

__all__ = ["ModelBackend", "ModelBackendError", "FakeModel", "OllamaBackend"]
