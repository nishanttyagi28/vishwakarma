"""Plugin discovery, custom gate runners and additive roles (A-010)."""

import types
from pathlib import Path

import pytest

from forge.config import GateSpec
from forge.gates import run_gate, run_gates_for_task
from forge.ledger import TaskLedger
from forge.permissions import FsGrants, RoleGrant
from forge.plugins import (
    REPO_ROOT,
    PluginError,
    PluginRegistry,
    discover_plugins,
    merge_roles,
)

PLUGIN_SOURCE = '''
from pathlib import Path
from forge.config import GateSpec
from forge.gates import GateOutcome
from forge.permissions import FsGrants, RoleGrant

def marker_gate(spec: GateSpec, cwd: Path, timeout_s: float) -> GateOutcome:
    passed = (cwd / "OK").is_file()
    return GateOutcome(gate=spec.name, command=spec.command or "plugin:demo.marker",
                       exit_code=0 if passed else 1, passed=passed,
                       output_tail="ok" if passed else "missing")

def register(registry):
    registry.add_gate_runner("demo.marker", marker_gate)
    registry.add_role(RoleGrant(
        role="demo_role", mission="demo",
        fs=FsGrants(read=["w:"], write=["w:docs/**"]),
        shell_allow=["git status"], tools=["fs"],
    ))
'''


def _write_plugin(tmp_path: Path) -> Path:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "demo_plugin.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    return plugin_dir


def test_folder_discovery_loads_runner_and_role(tmp_path):
    registry = discover_plugins(_write_plugin(tmp_path), use_entry_points=False)
    assert "demo.marker" in registry.gate_runners
    assert "demo_role" in registry.roles
    assert registry.roles["demo_role"].fs.write == ["w:docs/**"]
    assert registry.sources


def test_custom_gate_runner_passes_and_fails(tmp_path):
    registry = discover_plugins(_write_plugin(tmp_path), use_entry_points=False)
    workspace = tmp_path / "work"
    workspace.mkdir()
    spec = GateSpec(name="marker", runner="demo.marker", workspace="w")
    failed = run_gate(spec, workspace, gate_runners=registry.gate_runners)
    assert not failed.passed and failed.exit_code == 1
    (workspace / "OK").write_text("yes", encoding="utf-8")
    passed = run_gate(spec, workspace, gate_runners=registry.gate_runners)
    assert passed.passed and passed.exit_code == 0
    assert "ok" in passed.output_tail


def test_unknown_and_broken_runner_are_failed_gates(tmp_path):
    spec = GateSpec(name="missing", runner="no.such.runner", workspace="w")
    outcome = run_gate(spec, tmp_path, gate_runners={})
    assert not outcome.passed and outcome.exit_code == 127
    assert "unknown plugin gate runner" in outcome.output_tail

    def broken(spec, cwd, timeout_s):
        raise RuntimeError("boom")

    outcome = run_gate(spec, tmp_path, gate_runners={"no.such.runner": broken})
    assert not outcome.passed and outcome.exit_code == 127
    assert "boom" in outcome.output_tail


def test_plugin_gate_results_are_hash_stamped(tmp_path):
    registry = discover_plugins(_write_plugin(tmp_path), use_entry_points=False)
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "OK").write_text("yes", encoding="utf-8")
    ledger = TaskLedger(tmp_path / "l.db")
    try:
        ledger.create("P-1", "plugin gate", "backend")
        ledger.promote("P-1")
        ledger.claim("P-1", "run")
        ledger.submit_for_review("P-1", "backend", "done")
        ledger.request_gates("P-1")
        spec = GateSpec(name="marker", runner="demo.marker", workspace="w")
        report = run_gates_for_task(ledger, "P-1", [spec], {"w": str(workspace)},
                                    gate_runners=registry.gate_runners)
        assert report.passed
        assert len(ledger.gate_results("P-1")[0].payload_sha256) == 64
    finally:
        ledger.close()


def test_entry_point_discovery(monkeypatch, tmp_path):
    module = types.ModuleType("fake_plugin")

    def register(registry):
        registry.add_gate_runner("entry.pass", lambda spec, cwd, timeout: None)

    module.register = register  # type: ignore[attr-defined]

    class EntryPoint:
        name = "fake"

        def load(self):
            return module

    monkeypatch.setattr("forge.plugins._entry_points", lambda: [EntryPoint()])
    registry = discover_plugins(tmp_path, use_entry_points=True)
    assert "entry.pass" in registry.gate_runners
    assert any(source.startswith("entry-point:") for source in registry.sources)


def test_merge_roles_adds_but_never_overrides(tmp_path):
    registry = discover_plugins(_write_plugin(tmp_path), use_entry_points=False)
    base = {"backend": RoleGrant(role="backend", mission="built in",
                                 fs=FsGrants(read=["w:"]))}
    merged = merge_roles(base, registry)
    assert "demo_role" in merged and "backend" in merged

    conflict = PluginRegistry()
    conflict.add_role(RoleGrant(role="backend", mission="override attempt"))
    with pytest.raises(PluginError, match="conflicts with a built-in role"):
        merge_roles(base, conflict)


def test_duplicate_runner_names_are_refused():
    registry = PluginRegistry()
    registry.add_gate_runner("dup", lambda spec, cwd, timeout: None)
    with pytest.raises(PluginError, match="duplicate gate runner"):
        registry.add_gate_runner("dup", lambda spec, cwd, timeout: None)


def test_broken_plugin_file_fails_loudly(tmp_path):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "broken.py").write_text("import does_not_exist\n",
                                          encoding="utf-8")
    with pytest.raises(PluginError, match="failed to import"):
        discover_plugins(plugin_dir, use_entry_points=False)


def test_example_plugin_loads_from_repository_folder():
    registry = discover_plugins(REPO_ROOT / "plugins", use_entry_points=False)
    assert "example.marker" in registry.gate_runners
    assert "release_auditor" in registry.roles
