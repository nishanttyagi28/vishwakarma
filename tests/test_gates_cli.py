"""Tests for gate runners and the Forge CLI."""

import json
from pathlib import Path

import pytest
import yaml

from forge.cli import execute
from forge.config import GateSpec, load_config
from forge.gates import run_gate, run_gates_for_task
from forge.ledger import APPROVED, TESTING, LedgerError, TaskLedger

PY = "{python}"


@pytest.fixture()
def ws(tmp_path) -> Path:
    (tmp_path / "proj").mkdir()
    return tmp_path / "proj"


def ok_spec(ws_name="proj"):
    return GateSpec(name="echo-ok", command=f'{PY} -c "print(1)"', workspace=ws_name)


def fail_spec(ws_name="proj"):
    return GateSpec(name="echo-fail", command=f'{PY} -c "import sys; sys.exit(3)"',
                    workspace=ws_name)


def drive_to_review(ledger: TaskLedger, task_id="T-1"):
    ledger.create(task_id, "t", "backend")
    ledger.promote(task_id)
    ledger.claim(task_id, "run-1")
    ledger.submit_for_review(task_id, "backend", "done")


# --- gate runner ---------------------------------------------------------------


def test_run_gate_pass(ws):
    outcome = run_gate(ok_spec(), ws)
    assert outcome.passed and outcome.exit_code == 0


def test_run_gate_failure(ws):
    outcome = run_gate(fail_spec(), ws)
    assert not outcome.passed and outcome.exit_code == 3


def test_run_gate_missing_executable_is_a_failed_gate(ws):
    spec = GateSpec(name="missing", command="definitely-not-a-real-binary-xyz",
                    workspace="proj")
    outcome = run_gate(spec, ws)
    assert not outcome.passed
    assert outcome.exit_code == 127
    assert "could not start" in outcome.output_tail


def test_run_gate_missing_workspace_is_a_failed_gate(tmp_path):
    spec = GateSpec(name="no-cwd", command=f"{PY} -c \"print(1)\"", workspace="proj")
    outcome = run_gate(spec, tmp_path / "does-not-exist")
    assert not outcome.passed
    assert outcome.exit_code == 127
    assert "could not start" in outcome.output_tail


def test_run_gate_python_placeholder_uses_current_interpreter(ws):
    import sys

    spec = GateSpec(name="interp", command=f'{PY} -c "import sys; print(sys.executable)"',
                    workspace="proj")
    outcome = run_gate(spec, ws)
    assert outcome.passed
    assert Path(sys.executable).name in outcome.output_tail


def test_run_gate_unknown_workspace_fails_loudly():
    ledger = TaskLedger(Path(":memory:"))
    try:
        ledger.create("T-1", "t", "backend")
        with pytest.raises(ValueError):
            run_gates_for_task(ledger, "T-1", [ok_spec("ghost")], {"proj": "."})
    finally:
        ledger.close()


def test_run_gates_records_hash_stamped_results(ws, tmp_path):
    db = tmp_path / "l.db"
    ledger = TaskLedger(db)
    try:
        drive_to_review(ledger)
        report = run_gates_for_task(ledger, "T-1", [ok_spec()], {"proj": str(ws)})
        assert report.passed
        assert ledger.status_of("T-1") == TESTING
        results = ledger.gate_results("T-1")
        assert len(results) == 1
        payload = json.loads(results[0].payload)
        assert payload["gate"] == "echo-ok"
        assert len(results[0].payload_sha256) == 64
    finally:
        ledger.close()


def test_failing_gate_is_recorded_not_hidden(ws, tmp_path):
    db = tmp_path / "l.db"
    ledger = TaskLedger(db)
    try:
        drive_to_review(ledger)
        report = run_gates_for_task(ledger, "T-1", [ok_spec(), fail_spec()],
                                    {"proj": str(ws)})
        assert not report.passed
        assert ledger.status_of("T-1") == TESTING
        assert len(ledger.gate_results("T-1")) == 2
    finally:
        ledger.close()


def test_gates_refused_outside_review_or_testing(tmp_path):
    db = tmp_path / "l.db"
    ledger = TaskLedger(db)
    try:
        ledger.create("T-1", "t", "backend")  # BACKLOG
        with pytest.raises(ValueError):
            run_gates_for_task(ledger, "T-1", [ok_spec()], {"proj": "."})
    finally:
        ledger.close()


# --- CLI end-to-end -------------------------------------------------------------


@pytest.fixture()
def cli_env(tmp_path, ws):
    config_path = tmp_path / "forge.yaml"
    config = {
        "org": "TEST",
        "version": "0.0.0",
        "workspaces": {"proj": str(ws)},
        "model": {"default": "m", "lightweight": "m2"},
        "limits": {"max_steps_per_run": 10, "max_revisions_per_task": 1,
                   "max_retries_per_run": 1},
        "gates": {"proj": [
            {"name": "echo-ok", "command": f'{PY} -c "print(1)"', "workspace": "proj"},
        ]},
    }
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    db_path = tmp_path / "forge.sqlite3"
    return config_path, db_path


def test_cli_full_lifecycle_to_approved(cli_env, ws):
    config_path, db_path = cli_env
    execute(["task", "create", "--id", "T-1", "--title", "t", "--role", "backend"],
            config_path, db_path)
    execute(["task", "promote", "T-1"], config_path, db_path)
    execute(["task", "claim", "T-1", "--run-id", "run-1"], config_path, db_path)
    out = execute(["task", "submit", "T-1", "--actor", "backend", "--summary", "done"],
                  config_path, db_path)
    assert "REVIEW" in out
    out = execute(["gate", "run", "T-1", "--workspace", "proj"], config_path, db_path)
    assert "GATES PASSED" in out
    out = execute(["task", "approve", "T-1", "--actor", "chief", "--note", "ok"],
                  config_path, db_path)
    assert "APPROVED" in out
    ledger = TaskLedger(db_path)
    try:
        assert ledger.status_of("T-1") == APPROVED
        assert len(ledger.gate_results("T-1")) == 1
    finally:
        ledger.close()


def test_cli_failed_gate_exits_report_but_state_is_visible(cli_env, tmp_path):
    config_path, db_path = cli_env
    config = load_config(config_path)
    config.gates["proj"][0].command = f'{PY} -c "import sys; sys.exit(2)"'
    # write the modified config back for the gate run
    config_path.write_text(yaml.safe_dump(config.model_dump()), encoding="utf-8")
    execute(["task", "create", "--id", "T-1", "--title", "t", "--role", "backend"],
            config_path, db_path)
    execute(["task", "promote", "T-1"], config_path, db_path)
    execute(["task", "claim", "T-1", "--run-id", "run-1"], config_path, db_path)
    execute(["task", "submit", "T-1", "--actor", "backend", "--summary", "done"],
            config_path, db_path)
    out = execute(["gate", "run", "T-1", "--workspace", "proj"], config_path, db_path)
    assert "GATES FAILED" in out
    ledger = TaskLedger(db_path)
    try:
        assert ledger.status_of("T-1") == TESTING  # state visible, nothing hidden
        payload = json.loads(ledger.gate_results("T-1")[0].payload)
        assert payload["exit_code"] == 2
    finally:
        ledger.close()


def test_cli_human_gate_requires_founder(cli_env):
    config_path, db_path = cli_env
    execute(["task", "create", "--id", "T-2", "--title", "t", "--role", "backend",
             "--human-gate"], config_path, db_path)
    execute(["task", "promote", "T-2"], config_path, db_path)
    execute(["task", "claim", "T-2", "--run-id", "run-1"], config_path, db_path)
    execute(["task", "submit", "T-2", "--actor", "backend", "--summary", "done"],
            config_path, db_path)
    execute(["task", "gates", "T-2"], config_path, db_path)  # REVIEW -> TESTING
    with pytest.raises(LedgerError):
        execute(["task", "approve", "T-2", "--actor", "chief"], config_path, db_path)
    out = execute(["task", "approve", "T-2", "--actor", "founder", "--note", "ok"],
                  config_path, db_path)
    assert "APPROVED" in out


def test_cli_revision_budget_escalates(cli_env):
    config_path, db_path = cli_env
    execute(["task", "create", "--id", "T-3", "--title", "t", "--role", "backend"],
            config_path, db_path)
    execute(["task", "promote", "T-3"], config_path, db_path)
    execute(["task", "claim", "T-3", "--run-id", "run-1"], config_path, db_path)
    execute(["task", "submit", "T-3", "--actor", "backend", "--summary", "d"],
            config_path, db_path)
    # max_revisions=1 in this config: first change request OK, second escalates
    out = execute(["task", "changes", "T-3", "--actor", "chief", "--findings", "f1"],
                  config_path, db_path)
    assert "CHANGES_REQUESTED" in out
    execute(["task", "revise", "T-3", "--actor", "backend", "--plan", "p"],
            config_path, db_path)
    execute(["task", "submit", "T-3", "--actor", "backend", "--summary", "d2"],
            config_path, db_path)
    out = execute(["task", "changes", "T-3", "--actor", "chief", "--findings", "f2"],
                  config_path, db_path)
    assert "BLOCKED_HUMAN" in out


def test_cli_status_counts(cli_env):
    config_path, db_path = cli_env
    execute(["task", "create", "--id", "T-1", "--title", "t", "--role", "backend"],
            config_path, db_path)
    out = execute(["status"], config_path, db_path)
    assert "BACKLOG: 1" in out


def test_cli_show_and_history(cli_env):
    config_path, db_path = cli_env
    execute(["task", "create", "--id", "T-1", "--title", "mytitle", "--role", "backend",
             "--scope", "forge/", "--acceptance", "tests pass"], config_path, db_path)
    show = execute(["task", "show", "T-1"], config_path, db_path)
    assert "BACKLOG" in show and "mytitle" in show and "tests pass" in show
    execute(["task", "promote", "T-1"], config_path, db_path)
    history = execute(["task", "history", "T-1"], config_path, db_path)
    assert "BACKLOG -> READY" in history


def test_cli_env_overrides_select_config_and_db(tmp_path, monkeypatch, capsys):
    import sys

    from forge import cli as cli_mod

    cfg = tmp_path / "forge.yaml"
    cfg.write_text(yaml.safe_dump({
        "org": "T", "version": "0.0.0", "workspaces": {},
        "model": {"default": "m", "lightweight": "m"},
        "limits": {"max_steps_per_run": 3, "max_revisions_per_task": 1,
                   "max_retries_per_run": 1},
        "gates": {},
    }), encoding="utf-8")
    db = tmp_path / "other.sqlite3"
    monkeypatch.setenv("FORGE_CONFIG", str(cfg))
    monkeypatch.setenv("FORGE_DB", str(db))
    monkeypatch.setattr(sys, "argv",
                        ["forge.cli", "task", "create", "--id", "E-1",
                         "--title", "t", "--role", "backend"])
    cli_mod.main()
    out = capsys.readouterr().out
    assert "created E-1" in out
    assert db.exists()  # the env-selected ledger received the write


def test_workspace_paths_resolve_relative_to_config(tmp_path):
    (tmp_path / "proj").mkdir()
    path = tmp_path / "forge.yaml"
    path.write_text(yaml.safe_dump({
        "org": "T", "version": "0.0.0", "workspaces": {"proj": "proj"},
        "model": {"default": "m", "lightweight": "m"},
        "limits": {"max_steps_per_run": 3, "max_revisions_per_task": 1,
                   "max_retries_per_run": 1},
        "gates": {},
    }), encoding="utf-8")
    config = load_config(path)
    assert Path(config.workspaces["proj"]) == (tmp_path / "proj").resolve()
