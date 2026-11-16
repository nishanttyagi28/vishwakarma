# Plugins

Vishwakarma plugins add **custom gate runners** and **additive roles**. They are
trusted Python code, installed by an operator — never written or enabled by an
agent. Roles and gate specs in `forge/roles.yaml` / `forge.yaml` remain the
governance source of truth; plugins cannot override a built-in role.

## Discovery

Plugins are loaded from two places, in order:

1. Python entry points in the group `vishwakarma.plugins`.
2. Every `*.py` file in `plugins/` at the repository root (or the directory in
   `FORGE_PLUGINS_DIR`). Files starting with `_` are skipped.

A plugin module exposes `register(registry)` (or a `PLUGIN` object with a
`register` method). Broken imports, duplicate runner names and role-name
collisions raise `PluginError` — Forge fails loudly rather than silently
weakening a grant.

## Custom gate runner

```python
from pathlib import Path
from forge.config import GateSpec
from forge.gates import GateOutcome

def marker_gate(spec: GateSpec, cwd: Path, timeout_s: float) -> GateOutcome:
    passed = (cwd / "QUALITY_OK").is_file()
    return GateOutcome(
        gate=spec.name, command=spec.command or f"plugin:{spec.runner}",
        exit_code=0 if passed else 1, passed=passed,
        output_tail="marker found" if passed else "marker missing",
    )

def register(registry):
    registry.add_gate_runner("example.marker", marker_gate)
```

Reference it from `forge.yaml`:

```yaml
gates:
  vishwakarma:
    - name: quality-marker
      runner: example.marker
      workspace: vishwakarma
```

Runner exceptions and invalid return values are recorded as FAILED gates with
exit code 127; they never crash the trusted gate runner. Results are still
hash-stamped into the ledger like command gates.

## Additive role

```python
from forge.permissions import FsGrants, RoleGrant

def register(registry):
    registry.add_role(RoleGrant(
        role="release_auditor",
        mission="Audits release evidence and writes release notes.",
        fs=FsGrants(read=["vishwakarma:"],
                    write=["vishwakarma:docs/releases/**"]),
        shell_allow=["git status", "git log"],
        tools=["fs", "ledger.query"],
    ))
```

The CLI's `chief` command merges plugin roles into the YAML roles before any
agent is dispatched. A plugin role with the same name as a YAML role is
refused; expanding an existing role's authority still requires a governed
change to `forge/roles.yaml`.

## Example and tests

`plugins/example_gate.py` is a working example: the `example.marker` runner
passes when a workspace contains `RELEASE_OK` with the text `ok`, and it adds a
read-mostly `release_auditor` role. `tests/test_plugins.py` covers folder and
entry-point discovery, execution, failure handling and collision refusal.
