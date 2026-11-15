"""P-002 demo: Forge (VISHWAKARMA) builds a tiny CLI project end to end.

Uses the real Chief Engineer, ledger, prompt registry, journal, gates and
Forge Trace — with FakeModel adapters only (no Ollama; the laptop is
RAM-limited). The demo project lives in C:/Users/Admin/projects/
forge-demo-greet with its own git repo.

Run:  .venv/Scripts/python.exe scripts/demo_greet.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

DEMO = Path("C:/Users/Admin/projects/forge-demo-greet")
WS = "demo"
VENV_PY = str(Path(__file__).resolve().parents[1] / ".venv" / "Scripts" /
              "python.exe").replace("\\", "/")

SPEC = """# forge-demo-greet

A tiny deterministic CLI: `greet.py` exposes `greet(name)` returning
"Hello, <name>!" (name stripped and non-empty; empty raises ValueError).
Tests: `test_greet.py` with pytest.
"""

PA_REPLY = json.dumps({
    "context_selected": [f"{WS}:SPEC.md"],
    "instructions": "implement greet(name) in demo:greet.py plus test_greet.py "
                    "with pytest; follow the SPEC exactly",
    "uncertainties": [], "conflicts": [],
})

GREET_PY = (
    "def greet(name: str) -> str:\n"
    '    """Return a friendly greeting."""\n'
    "    clean = (name or '').strip()\n"
    "    if not clean:\n"
    "        raise ValueError('name must not be empty')\n"
    "    return f'Hello, {clean}!'\n"
    "\n\n"
    "if __name__ == '__main__':\n"
    "    import sys\n"
    "    print(greet(sys.argv[1] if len(sys.argv) > 1 else 'world'))\n"
)

TEST_PY = (
    "from greet import greet\n"
    "\n\n"
    "def test_greet_simple():\n"
    "    assert greet('Nishant') == 'Hello, Nishant!'\n"
    "\n\n"
    "def test_greet_strips():\n"
    "    assert greet('  Anu  ') == 'Hello, Anu!'\n"
    "\n\n"
    "def test_greet_rejects_empty():\n"
    "    import pytest\n"
    "    with pytest.raises(ValueError):\n"
    "        greet('   ')\n"
)

SP_WRITE = json.dumps({"thought": "write greet.py", "action": {
    "tool": "fs.write", "args": {"path": f"{WS}:greet.py", "content": GREET_PY}}})
TEST_WRITE = json.dumps({"thought": "write test_greet.py", "action": {
    "tool": "fs.write", "args": {"path": f"{WS}:test_greet.py", "content": TEST_PY}}})
SP_FINAL = json.dumps({"thought": "done", "final": {
    "summary": "implemented greet.py and test_greet.py per SPEC",
    "files_changed": [f"{WS}:greet.py", f"{WS}:test_greet.py"],
    "tests_run": ["python -m pytest -q (demo-pytest gate)"],
    "self_assessment": "three tests; strip + empty rejection covered",
    "open_issues": []}})

READ_TURN = json.dumps({"thought": "verify", "action": {
    "tool": "fs.read", "args": {"path": f"{WS}:greet.py"}}})
QA_CLEAN = json.dumps({"thought": "verdict", "final": {
    "summary": "reviewed greet.py against SPEC",
    "files_changed": [], "tests_run": [], "self_assessment": "ok",
    "open_issues": [],
    "artifacts": {"verdict": "CLEAN", "findings": []}}})


def main() -> int:
    from forge.chief import ChiefEngineer
    from forge.config import load_config
    from forge.journal import DecisionJournal
    from forge.ledger import APPROVED, TaskLedger
    from forge.model.fake import FakeModel
    from forge.permissions import FsGrants, PermissionChecker, RoleGrant
    from forge.prompting import PromptRegistry

    # --- demo workspace + spec -------------------------------------------
    DEMO.mkdir(parents=True, exist_ok=True)
    (DEMO / "SPEC.md").write_text(SPEC, encoding="utf-8")
    cfg_path = DEMO / "forge.yaml"
    cfg_path.write_text(
        "org: forge-demo\nversion: 0.0.0\n"
        f"workspaces:\n  {WS}: {DEMO.as_posix()}\n"
        "model:\n  default: fake\n  lightweight: fake\n"
        "limits:\n  max_steps_per_run: 12\n  max_revisions_per_task: 2\n"
        "  max_retries_per_run: 1\n"
        "gates:\n  demo:\n    - name: demo-pytest\n"
        f"      command: \"{VENV_PY} -m pytest -q\"\n      workspace: {WS}\n",
        encoding="utf-8",
    )
    config = load_config(cfg_path)

    grants = {
        "prompt_architect": RoleGrant(
            role="prompt_architect", mission="curate context",
            fs=FsGrants(read=[f"{WS}:"], write=[]),
            shell_allow=[], tools=["fs"],
        ),
        "backend": RoleGrant(
            role="backend", mission="implement the demo CLI",
            fs=FsGrants(read=[f"{WS}:"], write=[f"{WS}:"]),
            shell_allow=[],
            tools=["fs"],
        ),
        "qa": RoleGrant(
            role="qa", mission="review independently",
            fs=FsGrants(read=[f"{WS}:"], write=[]),
            shell_allow=[], tools=["fs"],
        ),
    }
    checker = PermissionChecker(grants, config.workspaces)

    ledger = TaskLedger(str(DEMO / "forge-demo.sqlite3"),
                        max_revisions=config.limits.max_revisions_per_task)
    registry = PromptRegistry(str(DEMO / "forge-prompts.sqlite3"))
    journal = DecisionJournal(str(DEMO / "forge-journal.sqlite3"))
    models = {
        "prompt_architect": FakeModel([PA_REPLY]),
        "backend": FakeModel([SP_WRITE, TEST_WRITE, SP_FINAL]),
        "qa": FakeModel([READ_TURN, QA_CLEAN]),
    }
    chief = ChiefEngineer(config, ledger, registry, journal, checker, models,
                          transcript_dir=DEMO / "runs", max_steps_per_run=12)

    ledger.create("DEMO-1", "implement greet(name) per demo:SPEC.md", "backend",
                  scope=f"{WS}:greet.py {WS}:test_greet.py",
                  exclusions="nothing else", acceptance="demo-pytest gate green")
    ledger.promote("DEMO-1", "founder")

    outcome = chief.execute_task("DEMO-1")
    print(f"[forge] DEMO-1 -> {outcome.status} ({outcome.outcome_class})")

    # --- evidence ----------------------------------------------------------
    if outcome.status != APPROVED:
        for h in ledger.handoffs_for("DEMO-1"):
            print(f"  {h.kind}: {h.sender}->{h.receiver} {h.status} "
                  f"verified={h.verified} {h.detail}")
        return 1

    print("\n--- forge trace: DEMO-1 handoff timeline ---")
    for h in ledger.handoffs_for("DEMO-1"):
        print(f"  [{h.seq:02d}] {h.kind:<9} {h.sender:>16} -> {h.receiver:<16}"
              f" status={h.status:<9} verified={h.verified} model={h.model}")

    print("\n--- gate results ---")
    for g in ledger.gate_results("DEMO-1"):
        print(f"  {g.payload.strip()[:100]}... (sha256 {g.payload_sha256[:16]})")

    print("\n--- the built project runs ---")
    built = subprocess.run(
        [VENV_PY, str(DEMO / "greet.py"), "Nishant"], capture_output=True, text=True)
    print(f"  $ greet.py Nishant -> {built.stdout.strip()}")
    tests = subprocess.run(
        [VENV_PY, "-m", "pytest", "-q", str(DEMO)], capture_output=True, text=True)
    print(f"  $ pytest -q {DEMO.name}: {tests.stdout.strip().splitlines()[-1]}")

    ledger.close()
    registry.close()
    journal.close()
    return 0


if __name__ == "__main__":
    if DEMO.exists():
        # deterministic re-run: clear build artifacts, keep SPEC/forge.yaml/.git
        for name in ("greet.py", "test_greet.py", "forge-demo.sqlite3",
                     "forge-prompts.sqlite3", "forge-journal.sqlite3"):
            p = DEMO / name
            if p.exists():
                p.unlink()
        for d in ("runs", "__pycache__"):
            shutil.rmtree(DEMO / d, ignore_errors=True)
    sys.exit(main())
