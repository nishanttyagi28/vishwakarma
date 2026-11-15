"""Forge configuration, loaded from ``forge.yaml`` at the repository root."""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ModelConfig(BaseModel):
    default: str
    lightweight: str


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


def load_config(path: Path) -> ForgeConfig:
    """Load and validate forge.yaml. Raises pydantic.ValidationError on bad config."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ForgeConfig.model_validate(data)
