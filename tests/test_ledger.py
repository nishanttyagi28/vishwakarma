"""Tests for the Forge task ledger and state machine."""

import json
import sqlite3

import pytest

from forge.ledger import (
    APPROVED,
    BACKLOG,
    BLOCKED_HUMAN,
    CHANGES_REQUESTED,
    IN_PROGRESS,
    READY,
    REVIEW,
    TERMINAL_STATES,
    TESTING,
    LedgerError,
    TaskLedger,
)


@pytest.fixture()
def ledger(tmp_path):
    led = TaskLedger(tmp_path / "forge.sqlite3", max_revisions=2)
    yield led
    led.close()


def make_task(ledger, task_id="T-1", depends_on=None, human_gate=False):
    return ledger.create(
        task_id,
        title="Test task",
        assignee_role="backend",
        depends_on=depends_on,
        human_gate=human_gate,
        scope="forge/",
        exclusions="no frontend files",
        acceptance="tests pass",
    )


def satisfy_approval(ledger, task_id="T-1", reviewer="qa", verdict="CLEAN",
                     gate="pytest", passed=True):
    """Record the evidence the ledger itself demands before approval."""
    ledger.record_gates(task_id, json.dumps({"gate": gate, "passed": passed}))
    ledger.record_handoff(
        task_id, sender=reviewer, receiver="chief", kind="verdict",
        verified=(verdict == "CLEAN" and passed),
        detail={"verdict": verdict, "findings": []},
    )


# --- creation and dependencies ------------------------------------------------


def test_create_starts_in_backlog(ledger):
    task = make_task(ledger)
    assert task.status == BACKLOG
    assert task.revisions_used == 0


def test_duplicate_task_id_refused(ledger):
    make_task(ledger)
    with pytest.raises(LedgerError):
        make_task(ledger)


def test_self_dependency_refused(ledger):
    with pytest.raises(LedgerError):
        make_task(ledger, task_id="T-x", depends_on=["T-x"])


def test_promote_blocked_until_dependencies_approved(ledger):
    make_task(ledger, task_id="T-dep")
    make_task(ledger, task_id="T-main", depends_on=["T-dep"])
    with pytest.raises(LedgerError):
        ledger.promote("T-main")
    ledger.promote("T-dep")
    ledger.claim("T-dep", "run-1")
    ledger.submit_for_review("T-dep", "backend", "done")
    ledger.request_gates("T-dep")
    satisfy_approval(ledger, "T-dep")
    ledger.approve("T-dep", "chief")
    ledger.promote("T-main")
    assert ledger.status_of("T-main") == READY


def test_promote_without_dependencies(ledger):
    make_task(ledger)
    ledger.promote("T-1")
    assert ledger.status_of("T-1") == READY


# --- atomic claiming ----------------------------------------------------------


def test_claim_moves_ready_to_in_progress(ledger):
    make_task(ledger)
    ledger.promote("T-1")
    task = ledger.claim("T-1", "run-42")
    assert task.status == IN_PROGRESS
    assert task.claimed_run == "run-42"


def test_claim_is_atomic_two_workers(ledger, tmp_path):
    """A second worker on the same db file cannot claim the claimed task."""
    make_task(ledger)
    ledger.promote("T-1")
    other = TaskLedger(tmp_path / "forge.sqlite3", max_revisions=2)
    try:
        ledger.claim("T-1", "run-1")
        with pytest.raises(LedgerError):
            other.claim("T-1", "run-2")
    finally:
        other.close()


def test_cannot_claim_unready_task(ledger):
    make_task(ledger)  # BACKLOG
    with pytest.raises(LedgerError):
        ledger.claim("T-1", "run-1")


# --- standard lifecycle -------------------------------------------------------


def drive_to_testing(ledger, task_id="T-1"):
    make_task(ledger, task_id=task_id)
    ledger.promote(task_id)
    ledger.claim(task_id, "run-1")
    ledger.submit_for_review(task_id, "backend", "implementation report")
    ledger.request_gates(task_id)


def test_happy_path_reaches_approved(ledger):
    drive_to_testing(ledger)
    satisfy_approval(ledger)
    task = ledger.approve("T-1", "chief", "gates green, review clean")
    assert task.status == APPROVED
    assert task.status in TERMINAL_STATES


def test_illegal_transition_refused(ledger):
    make_task(ledger)  # BACKLOG
    with pytest.raises(LedgerError):
        ledger.approve("T-1", "chief")  # cannot approve from BACKLOG


def test_cannot_approve_from_review(ledger):
    make_task(ledger)
    ledger.promote("T-1")
    ledger.claim("T-1", "run-1")
    ledger.submit_for_review("T-1", "backend", "done")
    with pytest.raises(LedgerError):
        ledger.approve("T-1", "chief")


def test_gate_results_only_in_testing(ledger):
    make_task(ledger)
    with pytest.raises(LedgerError):
        ledger.record_gates("T-1", "{}")


def test_gates_failed_can_return_to_review(ledger):
    drive_to_testing(ledger)
    ledger._transition("T-1", REVIEW, "chief", "gates failed")  # documented escape hatch
    assert ledger.status_of("T-1") == REVIEW


# --- revision loop and limits -------------------------------------------------


def drive_to_review(ledger):
    make_task(ledger)
    ledger.promote("T-1")
    ledger.claim("T-1", "run-1")
    ledger.submit_for_review("T-1", "backend", "done")


def test_revision_loop(ledger):
    drive_to_review(ledger)
    ledger.request_changes("T-1", "chief", "missing error handling")
    assert ledger.status_of("T-1") == CHANGES_REQUESTED
    ledger.start_revision("T-1", "backend", "add error handling")
    assert ledger.status_of("T-1") == IN_PROGRESS
    assert ledger.get("T-1").revisions_used == 1


def test_revision_budget_exhaustion_escalates(ledger):
    drive_to_review(ledger)
    ledger.request_changes("T-1", "chief", "still wrong")  # revision 1
    ledger.start_revision("T-1", "backend", "fix")
    ledger.submit_for_review("T-1", "backend", "done v2")
    ledger.request_changes("T-1", "chief", "still wrong v2")  # revision 2
    ledger.start_revision("T-1", "backend", "fix v2")
    ledger.submit_for_review("T-1", "backend", "done v3")
    task = ledger.request_changes("T-1", "chief", "still wrong v3")  # revision 3 -> blocked
    assert task.status == BLOCKED_HUMAN
    history = ledger.history("T-1")
    blocking = [t for t in history if t.to_status == BLOCKED_HUMAN]
    assert blocking, "no BLOCKED_HUMAN transition recorded"
    assert "revision budget exhausted" in blocking[-1].reason
    assert blocking[-1].from_status == REVIEW


# --- human gate ---------------------------------------------------------------


def test_human_gated_task_refuses_chief_approval(ledger):
    make_task(ledger, human_gate=True)
    ledger.promote("T-1")
    ledger.claim("T-1", "run-1")
    ledger.submit_for_review("T-1", "backend", "done")
    ledger.request_gates("T-1")
    with pytest.raises(LedgerError):
        ledger.approve("T-1", "chief")


def test_human_gated_task_approvable_by_founder(ledger):
    make_task(ledger, human_gate=True)
    ledger.promote("T-1")
    ledger.claim("T-1", "run-1")
    ledger.submit_for_review("T-1", "backend", "done")
    ledger.request_gates("T-1")
    satisfy_approval(ledger)
    task = ledger.approve("T-1", "founder", "founder decision")
    assert task.status == APPROVED


# --- escalation, cancellation, audit -----------------------------------------


def test_escalate_from_in_progress(ledger):
    make_task(ledger)
    ledger.promote("T-1")
    ledger.claim("T-1", "run-1")
    task = ledger.escalate("T-1", "backend", "ambiguous requirement")
    assert task.status == BLOCKED_HUMAN


def test_cancel_from_ready(ledger):
    make_task(ledger)
    ledger.promote("T-1")
    task = ledger.cancel("T-1", "founder", "obsolete")
    assert task.status == "CANCELLED"


def test_audit_trail_records_every_transition(ledger):
    drive_to_testing(ledger)
    satisfy_approval(ledger)
    ledger.approve("T-1", "chief")
    history = ledger.history("T-1")
    statuses = [t.to_status for t in history if t.from_status != t.to_status]
    assert statuses == [READY, IN_PROGRESS, REVIEW, TESTING, APPROVED]
    assert all(t.actor for t in history)
    assert history[0].from_status == BACKLOG


def test_audit_trail_is_append_only(ledger):
    """No UPDATE or DELETE may ever touch the audit tables - enforced by triggers."""
    drive_to_testing(ledger)
    satisfy_approval(ledger)  # ensure gate_results and handoffs have rows
    conn = sqlite3.connect(ledger.db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE transitions SET actor = 'ghost'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM transitions")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE gate_results SET payload = '{}'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM gate_results")
    finally:
        conn.close()


def test_gate_results_are_hash_stamped(ledger):
    drive_to_testing(ledger)
    result = ledger.record_gates("T-1", json.dumps({"gate": "pytest", "passed": True}))
    stored = ledger.gate_results("T-1")
    assert len(stored) == 1
    assert stored[0].payload_sha256 == result.payload_sha256
    assert len(result.payload_sha256) == 64


def test_unknown_task_refused(ledger):
    with pytest.raises(LedgerError):
        ledger.get("nope")


# --- approval invariants (enforced by the ledger, not the caller) -------------


def test_approve_requires_recorded_gate_results(ledger):
    drive_to_testing(ledger)
    with pytest.raises(LedgerError, match="no trusted gate results"):
        ledger.approve("T-1", "chief")


def test_approve_requires_a_clean_independent_review(ledger):
    drive_to_testing(ledger)
    ledger.record_gates("T-1", json.dumps({"gate": "pytest", "passed": True}))
    with pytest.raises(LedgerError, match="no independent review"):
        ledger.approve("T-1", "chief")


def test_approve_rejects_issues_verdict(ledger):
    drive_to_testing(ledger)
    satisfy_approval(ledger, verdict="ISSUES")
    with pytest.raises(LedgerError, match="not CLEAN"):
        ledger.approve("T-1", "chief")


def test_approve_rejects_self_review(ledger):
    drive_to_testing(ledger)
    satisfy_approval(ledger, reviewer="backend")
    with pytest.raises(LedgerError, match="not independent"):
        ledger.approve("T-1", "chief")


def test_latest_failed_gate_blocks_approval(ledger):
    drive_to_testing(ledger)
    satisfy_approval(ledger)  # an earlier pass
    ledger.record_gates("T-1", json.dumps({"gate": "pytest", "passed": False}))
    with pytest.raises(LedgerError, match="latest gate results failed"):
        ledger.approve("T-1", "chief")


def test_approve_requires_testing_state(ledger):
    make_task(ledger)
    ledger.promote("T-1")
    ledger.claim("T-1", "run-1")
    ledger.submit_for_review("T-1", "backend", "done")
    with pytest.raises(LedgerError, match="not TESTING"):
        ledger.approve("T-1", "chief")


def test_claim_is_one_transaction(ledger):
    """If the audit insert fails, the status update rolls back too."""
    make_task(ledger)
    ledger.promote("T-1")
    conn = sqlite3.connect(ledger.db_path)
    try:
        conn.execute(
            "CREATE TRIGGER fail_claim BEFORE INSERT ON transitions "
            "WHEN NEW.reason LIKE 'claimed by%' "
            "BEGIN SELECT RAISE(ABORT, 'simulated audit failure'); END;"
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(sqlite3.IntegrityError):
        ledger.claim("T-1", "run-1")
    assert ledger.status_of("T-1") == READY
    assert ledger.get("T-1").claimed_run is None
    assert not any("claimed by" in t.reason for t in ledger.history("T-1"))
