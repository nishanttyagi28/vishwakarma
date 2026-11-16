"""Persistent task ledger for VISHWAKARMA.

SQLite-backed. Four tables:

- ``tasks``         current task state, dependency lists, retry counters
- ``transitions``   append-only audit of every state change (who, why, when)
- ``gate_results``  hash-stamped gate payloads; written once, never updated
- ``handoffs``      append-only Forge Trace: every agent-to-agent boundary
                    event (sender, receiver, context, model, outcome,
                    verification). Written once, never updated.

The state machine is enforced here, not by agent judgement: an illegal
transition raises ``LedgerError``. Task claiming is atomic via a conditional
UPDATE, so two workers can never run the same task. Revision limits are
enforced automatically: a task that exceeds ``max_revisions_per_task``
escalates to BLOCKED_HUMAN instead of looping. Human-gated tasks can only be
approved by the ``founder`` actor.
"""

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel, Field

BACKLOG = "BACKLOG"
READY = "READY"
IN_PROGRESS = "IN_PROGRESS"
REVIEW = "REVIEW"
TESTING = "TESTING"
CHANGES_REQUESTED = "CHANGES_REQUESTED"
APPROVED = "APPROVED"
BLOCKED_HUMAN = "BLOCKED_HUMAN"
CANCELLED = "CANCELLED"

TERMINAL_STATES = {APPROVED, BLOCKED_HUMAN, CANCELLED}
FOUNDER = "founder"

#: (from, to) transitions the state machine accepts.
LEGAL_TRANSITIONS: set[tuple[str, str]] = {
    (BACKLOG, READY),
    (READY, IN_PROGRESS),
    (IN_PROGRESS, READY),  # recovery: claimed run died before completing
    (IN_PROGRESS, REVIEW),
    (REVIEW, TESTING),
    (REVIEW, CHANGES_REQUESTED),
    (CHANGES_REQUESTED, IN_PROGRESS),
    (TESTING, APPROVED),
    (TESTING, REVIEW),  # gates failed; back to review for a decision
    (BACKLOG, CANCELLED),
    (READY, CANCELLED),
    (IN_PROGRESS, CANCELLED),
    (REVIEW, CANCELLED),
    (READY, BLOCKED_HUMAN),
    (IN_PROGRESS, BLOCKED_HUMAN),
    (REVIEW, BLOCKED_HUMAN),
    (TESTING, BLOCKED_HUMAN),
    (BLOCKED_HUMAN, READY),  # governed unblock for retrying technical failures
}

#: transitions that additionally require every dependency to be APPROVED.
DEPENDENCY_GATED = {(BACKLOG, READY)}


class LedgerError(Exception):
    """An operation the ledger refuses (illegal transition, bad state, limits)."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class TaskRecord(BaseModel):
    id: str
    title: str
    assignee_role: str
    status: str
    depends_on: list[str] = Field(default_factory=list)
    human_gate: bool = False
    scope: str = ""
    exclusions: str = ""
    acceptance: str = ""
    revisions_used: int = 0
    retries_used: int = 0
    claimed_run: str | None = None
    created_at: str
    updated_at: str


class Transition(BaseModel):
    task_id: str
    from_status: str | None
    to_status: str
    actor: str
    reason: str
    at: str


class GateResult(BaseModel):
    task_id: str
    payload: str
    payload_sha256: str
    at: str


#: handoff kinds (Forge Trace, P-001)
KIND_DISPATCH = "dispatch"  # sender hands work to a receiver agent
KIND_REPORT = "report"  # receiver returned an implementation report
KIND_VERDICT = "verdict"  # reviewer returned a verdict
KIND_GATES = "gates"  # trusted gate runner result
KIND_FAILURE = "failure"  # receiver produced no valid result
KIND_RETRY = "retry"  # bounded retry after issues
KIND_BLOCK = "block"  # escalation to a human

HANDOFF_STATUSES = {"completed", "failed", "blocked", "pending"}


class HandoffRecord(BaseModel):
    """One agent-to-agent boundary event (Forge Trace)."""

    seq: int
    id: str
    task_id: str
    parent_id: str | None
    sender: str
    receiver: str
    kind: str
    objective: str = ""
    prompt_id: str = ""
    prompt_version: int = 0
    context_refs: list[str] = Field(default_factory=list)
    model: str = ""
    status: str = "completed"
    verified: bool = False
    detail: dict[str, object] = Field(default_factory=dict)
    at: str = ""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    assignee_role TEXT NOT NULL,
    status TEXT NOT NULL,
    depends_on TEXT NOT NULL DEFAULT '[]',
    human_gate INTEGER NOT NULL DEFAULT 0,
    scope TEXT NOT NULL DEFAULT '',
    exclusions TEXT NOT NULL DEFAULT '',
    acceptance TEXT NOT NULL DEFAULT '',
    revisions_used INTEGER NOT NULL DEFAULT 0,
    retries_used INTEGER NOT NULL DEFAULT 0,
    claimed_run TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transitions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gate_results (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS transitions_no_update
BEFORE UPDATE ON transitions
BEGIN SELECT RAISE(ABORT, 'transitions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS transitions_no_delete
BEFORE DELETE ON transitions
BEGIN SELECT RAISE(ABORT, 'transitions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS gate_results_no_update
BEFORE UPDATE ON gate_results
BEGIN SELECT RAISE(ABORT, 'gate_results are append-only'); END;
CREATE TRIGGER IF NOT EXISTS gate_results_no_delete
BEFORE DELETE ON gate_results
BEGIN SELECT RAISE(ABORT, 'gate_results are append-only'); END;
CREATE TABLE IF NOT EXISTS handoffs (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT NOT NULL UNIQUE,
  task_id TEXT NOT NULL,
  parent_id TEXT,
  sender TEXT NOT NULL,
  receiver TEXT NOT NULL,
  kind TEXT NOT NULL,
  objective TEXT NOT NULL DEFAULT '',
  prompt_id TEXT NOT NULL DEFAULT '',
  prompt_version INTEGER NOT NULL DEFAULT 0,
  context_refs TEXT NOT NULL DEFAULT '[]',
  model TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  verified INTEGER NOT NULL DEFAULT 0,
  detail TEXT NOT NULL DEFAULT '{}',
  at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS handoffs_no_update
BEFORE UPDATE ON handoffs
BEGIN SELECT RAISE(ABORT, 'handoffs are append-only'); END;
CREATE TRIGGER IF NOT EXISTS handoffs_no_delete
BEFORE DELETE ON handoffs
BEGIN SELECT RAISE(ABORT, 'handoffs are append-only'); END;
"""


class TaskLedger:
    """Persistent task ledger. One instance per process; file-backed."""

    def __init__(self, db_path: Path, max_revisions: int = 2) -> None:
        self.db_path = Path(db_path)
        self.max_revisions = max_revisions
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --- internal helpers ---------------------------------------------------

    def _row_to_task(self, row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            id=row["id"],
            title=row["title"],
            assignee_role=row["assignee_role"],
            status=row["status"],
            depends_on=json.loads(row["depends_on"]),
            human_gate=bool(row["human_gate"]),
            scope=row["scope"],
            exclusions=row["exclusions"],
            acceptance=row["acceptance"],
            revisions_used=row["revisions_used"],
            retries_used=row["retries_used"],
            claimed_run=row["claimed_run"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _get_row(self, task_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise LedgerError(f"unknown task {task_id!r}")
        return row

    def _transition(
        self, task_id: str, to_status: str, actor: str, reason: str
    ) -> TaskRecord:
        row = self._get_row(task_id)
        frm = row["status"]
        if (frm, to_status) not in LEGAL_TRANSITIONS:
            raise LedgerError(f"illegal transition {frm!r} -> {to_status!r}")
        if (frm, to_status) in DEPENDENCY_GATED:
            unmet = [d for d in json.loads(row["depends_on"]) if self.status_of(d) != APPROVED]
            if unmet:
                raise LedgerError(f"dependencies not APPROVED: {unmet}")
        now = _now()
        with self._conn:
            self._conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (to_status, now, task_id),
            )
            self._conn.execute(
                "INSERT INTO transitions (task_id, from_status, to_status, actor,"
                " reason, at) VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, frm, to_status, actor, reason, now),
            )
        return self.get(task_id)

    # --- public API ---------------------------------------------------------

    def create(
        self,
        task_id: str,
        title: str,
        assignee_role: str,
        depends_on: list[str] | None = None,
        human_gate: bool = False,
        scope: str = "",
        exclusions: str = "",
        acceptance: str = "",
    ) -> TaskRecord:
        for dep in depends_on or []:
            if dep == task_id:
                raise LedgerError("task cannot depend on itself")
        now = _now()
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO tasks (id, title, assignee_role, status, depends_on,"
                    " human_gate, scope, exclusions, acceptance, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        title,
                        assignee_role,
                        BACKLOG,
                        json.dumps(depends_on or []),
                        int(human_gate),
                        scope,
                        exclusions,
                        acceptance,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise LedgerError(f"task {task_id!r} already exists") from exc
        return self.get(task_id)

    def get(self, task_id: str) -> TaskRecord:
        return self._row_to_task(self._get_row(task_id))

    def status_of(self, task_id: str) -> str:
        return self._get_row(task_id)["status"]

    def list_tasks(self, status: str | None = None) -> list[TaskRecord]:
        if status is None:
            rows = self._conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY id", (status,)
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def promote(self, task_id: str, actor: str = FOUNDER) -> TaskRecord:
        """BACKLOG -> READY, only when every dependency is APPROVED."""
        return self._transition(task_id, READY, actor, "promoted to READY")

    def claim(self, task_id: str, run_id: str) -> TaskRecord:
        """Atomically claim a READY task for execution. Raises if not READY.

        The conditional UPDATE and its audit transition commit as one
        transaction: either the task is IN_PROGRESS with a matching history
        row, or nothing changed at all.
        """
        now = _now()
        with self._conn:
            cur = self._conn.execute(
                "UPDATE tasks SET status = ?, claimed_run = ?, updated_at = ?"
                " WHERE id = ? AND status = ?",
                (IN_PROGRESS, run_id, now, task_id, READY),
            )
            if cur.rowcount != 1:
                row = self._get_row(task_id)
                raise LedgerError(
                    f"task {task_id!r} not claimable (status={row['status']!r})"
                )
            self._conn.execute(
                "INSERT INTO transitions (task_id, from_status, to_status, actor, reason, at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, READY, IN_PROGRESS, "worker", f"claimed by run {run_id}", now),
            )
        return self.get(task_id)

    def submit_for_review(self, task_id: str, actor: str, summary: str) -> TaskRecord:
        return self._transition(task_id, REVIEW, actor, summary)

    def request_gates(self, task_id: str, actor: str = "chief") -> TaskRecord:
        return self._transition(task_id, TESTING, actor, "quality gates requested")

    def back_to_review(self, task_id: str, actor: str, reason: str) -> TaskRecord:
        """TESTING -> REVIEW (e.g. gates failed and a decision is needed)."""
        return self._transition(task_id, REVIEW, actor, reason)

    def recover_task(self, task_id: str, max_retries: int,
                     actor: str = "recovery") -> TaskRecord:
        """IN_PROGRESS -> READY after an interrupted run, with retry budgeting."""
        row = self._get_row(task_id)
        retries = row["retries_used"] + 1
        if retries > max_retries:
            with self._conn:
                self._conn.execute(
                    "UPDATE tasks SET retries_used = ?, updated_at = ? WHERE id = ?",
                    (retries, _now(), task_id),
                )
            return self._transition(
                task_id, BLOCKED_HUMAN, actor,
                f"retry budget exhausted after {retries - 1} interruptions",
            )
        with self._conn:
            self._conn.execute(
                "UPDATE tasks SET retries_used = ?, updated_at = ? WHERE id = ?",
                (retries, _now(), task_id),
            )
        return self._transition(
            task_id, READY, actor, f"recovered after interruption (retry {retries})"
        )

    def record_gates(self, task_id: str, payload: str, source: str = "gate_runner") -> GateResult:
        """Record a gate result. Written once with its hash; never updated."""
        row = self._get_row(task_id)
        if row["status"] != TESTING:
            raise LedgerError(f"gate results recorded only in TESTING, not {row['status']!r}")
        digest = sha256(payload.encode("utf-8")).hexdigest()
        now = _now()
        with self._conn:
            self._conn.execute(
                "INSERT INTO gate_results (task_id, payload, payload_sha256, at)"
                " VALUES (?, ?, ?, ?)",
                (task_id, payload, digest, now),
            )
            self._conn.execute(
                "INSERT INTO transitions (task_id, from_status, to_status, actor, reason, at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, TESTING, TESTING, source, f"gate result recorded ({digest[:12]})", now),
            )
        return GateResult(task_id=task_id, payload=payload, payload_sha256=digest, at=now)

    def latest_gate_outcomes(self, task_id: str) -> dict[str, bool]:
        """Latest recorded verdict per gate name (gate runner payloads only)."""
        latest: dict[str, bool] = {}
        for result in self.gate_results(task_id):
            try:
                data = json.loads(result.payload)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            name = data.get("gate")
            passed = data.get("passed")
            if isinstance(name, str) and isinstance(passed, bool):
                latest[name] = passed
        return latest

    def latest_verdict(self, task_id: str) -> HandoffRecord | None:
        """The most recent independent-review verdict handoff, if any."""
        for handoff in reversed(self.handoffs_for(task_id)):
            if handoff.kind == KIND_VERDICT:
                return handoff
        return None

    def _approval_blockers(self, task: TaskRecord) -> list[str]:
        """Reasons approval is refused. The ledger, not the caller, decides."""
        blockers: list[str] = []
        if task.status != TESTING:
            blockers.append(f"task is {task.status!r}, not TESTING")
        outcomes = self.latest_gate_outcomes(task.id)
        if not outcomes:
            blockers.append("no trusted gate results recorded")
        failed = sorted(name for name, passed in outcomes.items() if not passed)
        if failed:
            blockers.append(f"latest gate results failed: {failed}")
        verdict = self.latest_verdict(task.id)
        if verdict is None:
            blockers.append("no independent review verdict recorded")
        else:
            if verdict.sender == task.assignee_role:
                blockers.append("reviewer is the implementing role (not independent)")
            if str(verdict.detail.get("verdict", "")) != "CLEAN":
                blockers.append("latest review verdict is not CLEAN")
            if not verdict.verified:
                blockers.append("latest review verdict is not verified")
        return blockers

    def approve(self, task_id: str, actor: str, note: str = "") -> TaskRecord:
        """Approve only from TESTING with green gates and a CLEAN review.

        This invariant lives in the ledger itself, so no caller - CLI, Chief
        Engineer or future plugin - can approve unverified work.
        """
        task = self.get(task_id)
        if task.human_gate and actor != FOUNDER:
            raise LedgerError(
                f"task {task_id!r} is human-gated; only the founder may approve"
            )
        blockers = self._approval_blockers(task)
        if blockers:
            raise LedgerError(f"task {task_id!r} cannot be approved: " + "; ".join(blockers))
        return self._transition(task_id, APPROVED, actor, note or "approved")

    def request_changes(self, task_id: str, actor: str, findings: str) -> TaskRecord:
        """REVIEW -> CHANGES_REQUESTED; auto-escalates when the revision budget is spent."""
        row = self._get_row(task_id)
        revisions = row["revisions_used"] + 1
        if revisions > self.max_revisions:
            return self._transition(
                task_id,
                BLOCKED_HUMAN,
                actor,
                f"revision budget exhausted after {row['revisions_used']} revisions: {findings}",
            )
        with self._conn:
            self._conn.execute(
                "UPDATE tasks SET revisions_used = ?, updated_at = ? WHERE id = ?",
                (revisions, _now(), task_id),
            )
        return self._transition(task_id, CHANGES_REQUESTED, actor, findings)

    def start_revision(self, task_id: str, actor: str, plan: str) -> TaskRecord:
        return self._transition(task_id, IN_PROGRESS, actor, f"revision started: {plan}")

    def escalate(self, task_id: str, actor: str, reason: str) -> TaskRecord:
        return self._transition(task_id, BLOCKED_HUMAN, actor, reason)

    def cancel(self, task_id: str, actor: str, reason: str) -> TaskRecord:
        return self._transition(task_id, CANCELLED, actor, reason)

    def unblock(self, task_id: str, actor: str, reason: str) -> TaskRecord:
        """BLOCKED_HUMAN -> READY for a retry within the retry budget.

        Counts as a retry: retries_used increments, mirroring crash recovery.
        """
        row = self._get_row(task_id)
        with self._conn:
            self._conn.execute(
                "UPDATE tasks SET retries_used = ?, updated_at = ? WHERE id = ?",
                (row["retries_used"] + 1, _now(), task_id),
            )
        return self._transition(task_id, READY, actor, reason)

    # --- audit --------------------------------------------------------------

    def history(self, task_id: str) -> list[Transition]:
        rows = self._conn.execute(
            "SELECT * FROM transitions WHERE task_id = ? ORDER BY seq", (task_id,)
        ).fetchall()
        return [
            Transition(
                task_id=r["task_id"],
                from_status=r["from_status"],
                to_status=r["to_status"],
                actor=r["actor"],
                reason=r["reason"],
                at=r["at"],
            )
            for r in rows
        ]

    def gate_results(self, task_id: str) -> list[GateResult]:
        rows = self._conn.execute(
            "SELECT * FROM gate_results WHERE task_id = ? ORDER BY seq", (task_id,)
        ).fetchall()
        return [
            GateResult(
                task_id=r["task_id"],
                payload=r["payload"],
                payload_sha256=r["payload_sha256"],
                at=r["at"],
            )
            for r in rows
        ]

    # --- Forge Trace (P-001): agent-to-agent boundary events -----------------

    def record_handoff(
        self,
        task_id: str,
        *,
        sender: str,
        receiver: str,
        kind: str,
        parent_id: str | None = None,
        objective: str = "",
        prompt_id: str = "",
        prompt_version: int = 0,
        context_refs: list[str] | None = None,
        model: str = "",
        status: str = "completed",
        verified: bool = False,
        detail: dict[str, object] | None = None,
    ) -> str:
        """Append one handoff record; returns its id.

        Objective and detail are secret-redacted before persistence. A
        handoff is never self-declared verified: callers may only pass
        ``verified=True`` when an actual verification result (gates and
        independent review) backs it.
        """
        from .prompting.secrets import redact_text  # lazy: avoids import cycle
        if kind not in {
            KIND_DISPATCH, KIND_REPORT, KIND_VERDICT, KIND_GATES,
            KIND_FAILURE, KIND_RETRY, KIND_BLOCK,
        }:
            raise LedgerError(f"unknown handoff kind: {kind!r}")
        if status not in HANDOFF_STATUSES:
            raise LedgerError(f"unknown handoff status: {status!r}")
        if parent_id is not None:
            hit = self._conn.execute(
                "SELECT 1 FROM handoffs WHERE id = ? AND task_id = ?",
                (parent_id, task_id),
            ).fetchone()
            if hit is None:
                raise LedgerError(f"parent handoff {parent_id!r} not in this task")
        hid = f"h-{uuid.uuid4().hex[:12]}"
        blob = json.dumps(detail or {}, sort_keys=True)
        with self._conn:
            self._conn.execute(
                "INSERT INTO handoffs (id, task_id, parent_id, sender, receiver, "
                "kind, objective, prompt_id, prompt_version, context_refs, model, "
                "status, verified, detail, at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    hid, task_id, parent_id, sender, receiver, kind,
                    redact_text(objective), prompt_id, prompt_version,
                    json.dumps(context_refs or []), model, status,
                    int(verified), redact_text(blob), _now(),
                ),
            )
        return hid

    def handoffs_for(self, task_id: str) -> list[HandoffRecord]:
        rows = self._conn.execute(
            "SELECT * FROM handoffs WHERE task_id = ? ORDER BY seq", (task_id,)
        ).fetchall()
        return [_handoff_from_row(r) for r in rows]

    def failed_handoffs(self) -> list[HandoffRecord]:
        """All failed or blocked handoffs across tasks, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM handoffs WHERE status IN ('failed','blocked') "
            "ORDER BY seq DESC"
        ).fetchall()
        return [_handoff_from_row(r) for r in rows]

    def get_handoff(self, hid: str) -> HandoffRecord:
        row = self._conn.execute(
            "SELECT * FROM handoffs WHERE id = ?", (hid,)
        ).fetchone()
        if row is None:
            raise LedgerError(f"unknown handoff: {hid!r}")
        return _handoff_from_row(row)


def _handoff_from_row(r: sqlite3.Row) -> HandoffRecord:
    return HandoffRecord(
        seq=r["seq"],
        id=r["id"],
        task_id=r["task_id"],
        parent_id=r["parent_id"],
        sender=r["sender"],
        receiver=r["receiver"],
        kind=r["kind"],
        objective=r["objective"],
        prompt_id=r["prompt_id"],
        prompt_version=r["prompt_version"],
        context_refs=json.loads(r["context_refs"]),
        model=r["model"],
        status=r["status"],
        verified=bool(r["verified"]),
        detail=json.loads(r["detail"]),
        at=r["at"],
    )
