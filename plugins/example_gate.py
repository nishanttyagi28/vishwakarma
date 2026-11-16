"""Example Vishwakarma plugin: a marker-file gate and an additive role.

Copy this file into ``plugins/`` (or expose it as a ``vishwakarma.plugins``
entry point) to load it. Gate runner plugins receive the GateSpec, workspace
path and timeout, and return a ``forge.gates.GateOutcome``.
"""

from pathlib import Path

from forge.config import GateSpec
from forge.gates import GateOutcome
from forge.permissions import FsGrants, RoleGrant
from forge.plugins import PluginRegistry


def release_marker_gate(spec: GateSpec, cwd: Path, timeout_s: float) -> GateOutcome:
    """Pass when the workspace contains a RELEASE_OK file with 'ok'."""
    marker = cwd / "RELEASE_OK"
    try:
        content = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
    except OSError as exc:
        content = f"<unreadable: {exc}>"
    passed = content == "ok"
    return GateOutcome(
        gate=spec.name,
        command=spec.command or f"plugin:{spec.runner}",
        exit_code=0 if passed else 1,
        passed=passed,
        output_tail=("release marker ok" if passed
                     else f"missing or invalid {marker.name}: {content!r}"),
    )


def register(registry: PluginRegistry) -> None:
    registry.add_gate_runner("example.marker", release_marker_gate)
    registry.add_role(RoleGrant(
        role="release_auditor",
        mission=(
            "Audits release evidence and writes release notes; never changes "
            "product code or governance files."
        ),
        fs=FsGrants(
            read=["vishwakarma:", "smoke:"],
            write=["vishwakarma:docs/releases/**"],
        ),
        shell_allow=["git status", "git log", "git diff"],
        tools=["fs", "ledger.query"],
    ))
