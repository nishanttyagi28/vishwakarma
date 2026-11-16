"""Trusted gate runners.

Gates are commands declared in ``forge.yaml`` (never by an agent) and executed
here, by Forge itself — the gate runner is the only writer of gate results in
the ledger, and every result is hash-stamped on write. Agents may read gate
results; they cannot produce, edit or delete them.
"""

import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from forge.config import GateSpec
from forge.ledger import REVIEW, TESTING, TaskLedger


class GateOutcome(BaseModel):
    gate: str
    command: str
    exit_code: int
    passed: bool
    output_tail: str


@dataclass
class GatesReport:
    passed: bool
    outcomes: list[GateOutcome]

    def summary(self) -> str:
        lines = []
        for o in self.outcomes:
            verdict = "PASS" if o.passed else "FAIL"
            lines.append(f"{verdict} {o.gate} (exit {o.exit_code})")
        return "\n".join(lines)


def run_gate(spec: GateSpec, cwd: Path, timeout_s: float = 900.0) -> GateOutcome:
    """Execute one gate command. Exit code 0 = pass; anything else = fail.

    Commands are split with POSIX shlex and run without a shell: gate specs
    must be simple argv-style commands (no pipes or redirection), and forward
    slashes work on Windows because CreateProcess accepts them directly.
    """
    argv = shlex.split(spec.command, posix=True)
    # ``{python}`` is replaced with the interpreter running Forge, so gate
    # commands never depend on a bare "python" being on PATH or on a
    # platform-specific virtualenv layout (Scripts vs bin).
    argv = [sys.executable if token == "{python}" else token for token in argv]
    if argv and argv[0] in ("python", "python3"):
        argv[0] = sys.executable
    if not argv:
        return GateOutcome(
            gate=spec.name, command=spec.command, exit_code=127, passed=False,
            output_tail="gate command is empty",
        )
    # CreateProcess resolves the executable against the parent's cwd, not the
    # child's, so anchor a relative argv[0] to the gate's workspace explicitly.
    if ("/" in argv[0] or "\\" in argv[0]) and not Path(argv[0]).is_absolute():
        candidate = cwd / argv[0]
        if candidate.exists():
            argv[0] = str(candidate)
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        exit_code, output = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        exit_code, output = -1, f"gate timed out after {timeout_s}s"
    except (FileNotFoundError, OSError) as exc:
        # A missing interpreter or unreadable workspace is a failed gate, not
        # a crash of the trusted runner. Record it like any other failure.
        exit_code, output = 127, f"gate could not start: {exc}"
    return GateOutcome(
        gate=spec.name,
        command=spec.command,
        exit_code=exit_code,
        passed=exit_code == 0,
        output_tail=output[-2000:],
    )


def run_gates_for_task(
    ledger: TaskLedger,
    task_id: str,
    specs: list[GateSpec],
    workspaces: dict[str, str],
    timeout_s: float = 900.0,
) -> GatesReport:
    """Run all gates for a task and record each result in the ledger.

    If the task is in REVIEW, it is moved to TESTING first (as the chief
    would). Tasks already in TESTING simply gain more recorded results.
    Tasks elsewhere are refused by the ledger's state machine.
    """
    if ledger.status_of(task_id) == REVIEW:
        ledger.request_gates(task_id, actor="chief")
    if ledger.status_of(task_id) != TESTING:
        raise ValueError(f"task {task_id!r} must be in TESTING to run gates")

    outcomes: list[GateOutcome] = []
    for spec in specs:
        try:
            cwd = Path(workspaces[spec.workspace])
        except KeyError:
            raise ValueError(
                f"gate {spec.name!r} references unknown workspace {spec.workspace!r}"
            ) from None
        outcome = run_gate(spec, cwd, timeout_s)
        payload = json.dumps(outcome.model_dump(), sort_keys=True)
        ledger.record_gates(task_id, payload, source=f"gate_runner:{spec.name}")
        outcomes.append(outcome)
    return GatesReport(passed=all(o.passed for o in outcomes), outcomes=outcomes)
