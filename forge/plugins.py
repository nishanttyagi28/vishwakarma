"""Plugin discovery for custom gate runners and additive roles (A-010).

Plugins are trusted Python code, not agent input. They can be discovered from:

1. the ``vishwakarma.plugins`` entry-point group, and
2. a ``plugins/`` folder (default: repository root, override with
   ``FORGE_PLUGINS_DIR``).

A plugin module exposes ``register(registry)`` (or a ``PLUGIN`` object with a
``register`` method). It may add gate runners and new roles. Plugins cannot
replace or weaken a built-in role: role-name collisions are refused.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import os
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from forge.config import GateSpec
from forge.permissions import RoleGrant

if TYPE_CHECKING:
    from forge.gates import GateOutcome

ENTRY_POINT_GROUP = "vishwakarma.plugins"
REPO_ROOT = Path(__file__).resolve().parents[1]

GateRunner = Callable[[GateSpec, Path, float], "GateOutcome"]


class PluginError(Exception):
    """A plugin is malformed, duplicated, or conflicts with built-in governance."""


@dataclass
class PluginRegistry:
    """The capability surface plugins may extend."""

    gate_runners: dict[str, GateRunner] = field(default_factory=dict)
    roles: dict[str, RoleGrant] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)

    def add_gate_runner(self, name: str, runner: GateRunner) -> None:
        if not name.strip():
            raise PluginError("gate runner name must not be empty")
        if name in self.gate_runners:
            raise PluginError(f"duplicate gate runner {name!r}")
        self.gate_runners[name] = runner

    def add_role(self, grant: RoleGrant) -> None:
        if grant.role in self.roles:
            raise PluginError(f"duplicate plugin role {grant.role!r}")
        self.roles[grant.role] = grant


def _register_module(module: types.ModuleType, registry: PluginRegistry,
                     source: str) -> None:
    register = getattr(module, "register", None)
    if register is None:
        plugin = getattr(module, "PLUGIN", None)
        register = getattr(plugin, "register", None)
    if not callable(register):
        raise PluginError(f"plugin {source} has no register(registry) function")
    register(registry)
    registry.sources.append(source)


def load_plugin_file(path: Path, registry: PluginRegistry) -> None:
    module_name = f"forge_plugin_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PluginError(f"cannot load plugin file {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise PluginError(f"plugin {path} failed to import: {exc}") from exc
    _register_module(module, registry, str(path))


def _entry_points() -> list[importlib.metadata.EntryPoint]:
    points = importlib.metadata.entry_points()
    if hasattr(points, "select"):
        return list(points.select(group=ENTRY_POINT_GROUP))
    legacy_get = getattr(points, "get", None)
    if legacy_get is not None:  # pragma: no cover - old importlib.metadata
        return list(legacy_get(ENTRY_POINT_GROUP, []))
    return []


def load_entry_points(registry: PluginRegistry) -> None:
    for entry in _entry_points():
        try:
            module = entry.load()
        except Exception as exc:
            raise PluginError(
                f"entry-point plugin {entry.name!r} failed to load: {exc}"
            ) from exc
        _register_module(module, registry, f"entry-point:{entry.name}")


def discover_plugins(
    plugins_dir: Path | str | None = None,
    use_entry_points: bool = True,
) -> PluginRegistry:
    """Load entry points, then every ``*.py`` in the plugins folder."""
    registry = PluginRegistry()
    if use_entry_points:
        load_entry_points(registry)
    if plugins_dir is None:
        plugins_dir = os.environ.get("FORGE_PLUGINS_DIR", str(REPO_ROOT / "plugins"))
    directory = Path(plugins_dir)
    if directory.is_dir():
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            load_plugin_file(path, registry)
    return registry


def merge_roles(base: dict[str, RoleGrant],
                registry: PluginRegistry) -> dict[str, RoleGrant]:
    """Add plugin roles; never override a built-in role."""
    merged = dict(base)
    for name, grant in registry.roles.items():
        if name in merged:
            raise PluginError(
                f"plugin role {name!r} conflicts with a built-in role"
            )
        merged[name] = grant
    return merged
