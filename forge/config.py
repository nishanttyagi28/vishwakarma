"""Forge configuration, loaded from ``forge.yaml`` at the repository root."""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ModelConfig(BaseModel):
    default: str
    lightweight: str


class GateSpec(BaseModel):
    name: str
    command: str
    workspace: str


class LimitsConfig(BaseModel):
    max_steps_per_run: int = Field(default=30, ge=1)
    max_revisions_per_task: int = Field(default=2, ge=0)
    max_retries_per_run: int = Field(default=2, ge=0)


class ForgeConfig(BaseModel):
    org: str
    version: str
    workspaces: dict[str, str]
    model: ModelConfig
    limits: LimitsConfig
    gates: dict[str, list[GateSpec]] = Field(default_factory=dict)


def load_config(path: Path) -> ForgeConfig:
    """Load and validate forge.yaml. Raises pydantic.ValidationError on bad config.

    Workspace paths are resolved relative to the directory containing the
    config file, so a checked-out repository is portable across machines and
    platforms (no absolute paths in forge.yaml).
    """
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    config = ForgeConfig.model_validate(data)
    base = path.resolve().parent
    config.workspaces = {
        name: str((base / workspace).resolve()) if not Path(workspace).is_absolute()
        else workspace
        for name, workspace in config.workspaces.items()
    }
    return config
