"""VISHWAKARMA Chief Engineer runtime.

The CE loop is deterministic mechanism (ADR-0003): task selection from the
roadmap, dependency-aware promotion, PA package preparation, specialist
dispatch, trusted gates, independent reviewer dispatch, approval rules and
bounded revision loops are all code. LLM agents are invoked only where
judgement is needed — PA curation, specialist implementation, reviewer
findings — and every agent run is bounded and transcripted.

Approval rule: APPROVED requires green trusted gates AND a CLEAN independent
review. Anything else becomes a bounded revision; exhausting the ledger's
revision budget escalates to BLOCKED_HUMAN automatically. Human-gated tasks
stop in TESTING awaiting the founder.

The CE never writes product source: specialists do, through permission-checked
tools; reviewers are a different role than the implementer.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from forge.config import ForgeConfig
from forge.gates import GatesReport, run_gates_for_task
from forge.journal import DecisionJournal
from forge.ledger import (
    APPROVED,
    BACKLOG,
    BLOCKED_HUMAN,
    IN_PROGRESS,
    KIND_BLOCK,
    KIND_DISPATCH,
    KIND_FAILURE,
    KIND_GATES,
    KIND_REPORT,
    KIND_RETRY,
    KIND_VERDICT,
    LedgerError,
    TaskLedger,
    TaskRecord,
)
from forge.model.base import Message, ModelBackend
from forge.permissions import PermissionChecker
from forge.prompting import (
    APPROVED_FIRST_PASS,
    REVISED_ONCE,
    REVISED_TWICE,
    Curation,
    PackageValidationError,
    PromptRegistry,
    assemble,
    gather_candidates,
    render,
)
from forge.prompting import (
    BLOCKED_HUMAN as OUTCOME_BLOCKED,
)
from forge.prompting.package import TaskPackage, package_hash
from forge.runtime.agent import COMPLETED, AgentRuntime, RunResult
from forge.runtime.tools import ToolExecutor


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _tag() -> str:
    """Filesystem-safe timestamp for run IDs (Windows forbids ':' in names)."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


class TaskSpec(BaseModel):
    id: str
    title: str
    role: str
    depends: list[str] = Field(default_factory=list)
    human_gate: bool = False
    scope: str = ""
    exclusions: str = ""
    acceptance: str = ""


class Roadmap(BaseModel):
    tasks: list[TaskSpec] = Field(default_factory=list)


class TaskOutcome(BaseModel):
    task_id: str
    status: str
    outcome_class: str = ""
    detail: str = ""


@dataclass
class _Attempt:
    """Result of one specialist + gates + review attempt."""

    outcome: TaskOutcome | None = None
    feedback: str = ""
    parent_id: str = ""


def load_roadmap(path: Path) -> Roadmap:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Roadmap.model_validate({"tasks": data.get("tasks", [])})


class ChiefEngineer:
    def __init__(
        self,
        config: ForgeConfig,
        ledger: TaskLedger,
        registry: PromptRegistry,
        journal: DecisionJournal,
        checker: PermissionChecker,
        models: dict[str, ModelBackend],
        transcript_dir: Path,
        reviewer_role: str = "qa",
        token_cap: int = 12000,
        max_steps_per_run: int = 30,
    ) -> None:
        self.config = config
        self.ledger = ledger
        self.registry = registry
        self.journal = journal
        self.checker = checker
        self.models = models
        self.transcript_dir = transcript_dir
        self.reviewer_role = reviewer_role
        self.token_cap = token_cap
        self.max_steps = max_steps_per_run

    # --- PA pipeline ----------------------------------------------------------
    # ADR-0004: PA curation is single-shot structured judgement, not a ReAct
    # loop. The 6.7B local model adheres to one-shot JSON schemas far more
    # reliably than to multi-turn tool protocols, and curation needs no tools:
    # candidates are gathered deterministically. Specialists and reviewers keep
    # the bounded loop because they act with tools.

    def _single_shot_json(self, model: ModelBackend, prompt: str,
                          attempts: int = 3) -> dict[str, object]:
        """One-shot structured judgement: ask, validate, retry with the error."""
        messages: list[Message] = [
            {"role": "system",
             "content": "You reply with exactly one JSON object and no other text."},
            {"role": "user", "content": prompt},
        ]
        last_error = ""
        for _ in range(attempts):
            raw = model.chat(messages, json_mode=True)
            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("reply is not a JSON object")
                return data
            except ValueError as exc:
                last_error = str(exc)
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": f"Invalid reply ({last_error}). Reply again with "
                               "exactly the required JSON object and no other text.",
                })
        raise RuntimeError(f"structured reply failed after {attempts} attempts: {last_error}")

    def prepare_package(self, task: TaskRecord) -> tuple[TaskPackage, RunResult]:
        candidates = gather_candidates(task, self.config)
        directive = (
            f"Task {task.id} for the {task.assignee_role} agent.\n"
            f"Objective: {task.title}\n"
            f"Scope: {task.scope or '-'}\nExclusions: {task.exclusions or '-'}\n"
            "Candidate files (deterministic retrieval):\n"
            + "\n".join(f"- {c}" for c in candidates or ["(none)"])
            + "\n\nUse ONLY exact paths from the candidate list above; never invent "
            "paths. Select the minimum sufficient context. Reply with ONE JSON object:\n"
            '{"context_selected": ["workspace:path", ...], "instructions": "...", '
            '"uncertainties": ["..."], "conflicts": ["..."]}'
        )
        # prompt identity includes the task attempt: retries are governed by
        # the ledger's retry budget, while the registry's revision budget
        # guards within-attempt prompt optimisation only.
        attempt_no = task.retries_used
        prompt_id = f"p-{task.id}" if attempt_no == 0 else f"p-{task.id}-r{attempt_no}"
        feedback = ""
        for correction in range(2):
            data = self._single_shot_json(self.models["prompt_architect"],
                                          directive + feedback)
            raw_ctx = data.get("context_selected", [])
            unc = data.get("uncertainties", [])
            conf = data.get("conflicts", [])
            curation = Curation(
                context_selected=[str(p) for p in raw_ctx]
                if isinstance(raw_ctx, list) else [],
                instructions=str(data.get("instructions", "")),
                uncertainties=[str(u) for u in unc]
                if isinstance(unc, list) else [],
                conflicts=[str(c) for c in conf]
                if isinstance(conf, list) else [],
            )
            try:
                gate_workspaces = (
                    sorted({p.split(":", 1)[0] for p in curation.context_selected
                            if ":" in p})
                    or None
                )
                package = assemble(
                    task, curation, self.checker, self.config, f"pkg-{task.id}",
                    prompt_id=prompt_id, prompt_version=0, token_cap=self.token_cap,
                    gate_workspaces=gate_workspaces,
                )
            except PackageValidationError as exc:
                feedback = (
                    "\n\nYour previous reply FAILED deterministic validation:\n"
                    + "\n".join(f"- {e}" for e in exc.errors)
                    + "\nFix context_selected using EXACT candidate paths from the "
                    "list above. Reply with the corrected ONE JSON object."
                )
                continue
            version = self._register_prompt(prompt_id, package, task)
            package = package.model_copy(update={"prompt_version": version})
            return package, RunResult(
                run_id=f"pa-{task.id}-{_tag()}", role="prompt_architect",
                status=COMPLETED, report=None, steps_used=correction + 1,
                transcript_path="",
            )
        raise RuntimeError("package validation failed after PA correction attempt")

    def _register_prompt(self, prompt_id: str, package: TaskPackage,
                         task: TaskRecord) -> int:
        version = self.registry.register(
            prompt_id, task.assignee_role, "implementation", package_hash(package)
        )
        if version > 1:
            self.registry.supersede(prompt_id, version - 1, version)
        return version

    # --- specialist and reviewer runs -----------------------------------------

    def _default_workspace(self, package: TaskPackage) -> str | None:
        prefixes = [cf.path.split(":", 1)[0] for cf in package.context_files
                    if ":" in cf.path]
        if not prefixes:
            return None
        return max(set(prefixes), key=prefixes.count)

    def _run_specialist(self, task: TaskRecord, package: TaskPackage, run_id: str,
                        feedback: str = "") -> RunResult:
        role = task.assignee_role
        tools = ToolExecutor(self.checker, role,
                             default_workspace=self._default_workspace(package),
                             ledger=self.ledger)
        agent = AgentRuntime(
            model=self.models[role],
            tools=tools,
            role=role,
            mission=self.checker.grants[role].mission,
            max_steps=self.max_steps,
            transcript_dir=self.transcript_dir,
        )
        feedback_block = ""
        if feedback:
            feedback_block = "\n\nREVIEW FEEDBACK (address exactly these):\n" + feedback

        context = render(package, self.checker) + feedback_block
        return agent.run(run_id, context)

    def _run_reviewer(self, task: TaskRecord, specialist: RunResult,
                      gates: GatesReport, run_id: str) -> RunResult:
        role = self.reviewer_role
        tools = ToolExecutor(self.checker, role, ledger=self.ledger)
        files = specialist.report.files_changed if specialist.report else []
        agent = AgentRuntime(
            model=self.models[role],
            tools=tools,
            role=role,
            mission=self.checker.grants[role].mission,
            max_steps=min(self.max_steps, 8),
            transcript_dir=self.transcript_dir,
        )
        context = (
            f"INDEPENDENT REVIEW of task {task.id}\n"
            f"Objective: {task.title}\n"
            f"Gate summary:\n{gates.summary()}\n"
            "The specialist reported changing exactly these files:\n"
            + ("\n".join(f"- {f}" for f in files) or "- (none declared)")
            + "\n\nRead EXACTLY these files with fs.read (using their full "
            "'workspace:path' form) and verify the work. Do not read anything "
            "else. If a declared file cannot be read, that is itself a finding.\n"
            "Reply per protocol; in your final report set artifacts: "
            "{verdict: CLEAN|ISSUES, findings: [...]}."
        )
        return agent.run(run_id, context)

    # --- core loop --------------------------------------------------------------

    # --- Forge Trace (P-001) ---------------------------------------------------

    def _model_name(self, role: str) -> str:
        m = self.models[role]
        return str(getattr(m, "model_name", None) or getattr(m, "model", None)
                   or type(m).__name__)

    def _tool_summary(self, transcript_path: str) -> dict[str, int]:
        """Aggregate tool usage from a run transcript (best effort)."""
        if not transcript_path:
            return {}
        counts: dict[str, int] = {}
        try:
            with open(transcript_path, encoding="utf-8") as fh:
                for line in fh:
                    rec = json.loads(line)
                    if rec.get("type") == "tool":
                        name = str(rec.get("tool", "?"))
                        counts[name] = counts.get(name, 0) + 1
        except (OSError, ValueError):
            return {}
        return counts

    def _trace(self, task_id: str, *, sender: str, receiver: str, kind: str,
               parent: str | None, objective: str = "", prompt_id: str = "",
               prompt_version: int = 0, context_refs: list[str] | None = None,
               model: str = "", status: str = "completed",
               verified: bool = False,
               detail: dict[str, object] | None = None) -> str:
        return self.ledger.record_handoff(
            task_id, sender=sender, receiver=receiver, kind=kind,
            parent_id=parent, objective=objective, prompt_id=prompt_id,
            prompt_version=prompt_version, context_refs=context_refs or [],
            model=model, status=status, verified=verified, detail=detail,
        )

    def _ensure_running(self, task_id: str) -> tuple[TaskRecord | None, TaskOutcome | None]:
        """Promote/claim as needed; return (task, early outcome)."""
        task = self.ledger.get(task_id)
        if task.status == BACKLOG:
            try:
                self.ledger.promote(task_id)
            except LedgerError as exc:
                return None, TaskOutcome(
                    task_id=task_id, status=self.ledger.status_of(task_id),
                    detail=f"cannot promote: {exc}",
                )
        status = self.ledger.status_of(task_id)
        if status in (BLOCKED_HUMAN, APPROVED):
            return None, TaskOutcome(task_id=task_id, status=status,
                                     detail="terminal state, skipped")
        if status != IN_PROGRESS:
            self.ledger.claim(task_id, f"chief-{_tag()}")
        return self.ledger.get(task_id), None

    def _prepare_task(
        self, task: TaskRecord
    ) -> tuple[TaskPackage | None, str | None, TaskOutcome | None]:
        """Run PA curation and record its dispatch; return (package, id, failure)."""
        self.journal.record(
            "dispatch",
            f"delegated {task.id} to {task.assignee_role} via PA-prepared package",
            task_id=task.id,
        )
        try:
            package, _pa_result = self.prepare_package(task)
        except (RuntimeError, PackageValidationError) as exc:
            fail_id = self._trace(
                task.id, sender="chief", receiver="prompt_architect",
                kind=KIND_FAILURE, parent=None, objective=task.title,
                model=self._model_name("prompt_architect"), status="failed",
                detail={"error": str(exc), "stage": "package_preparation"},
            )
            out = self.ledger.escalate(task.id, "chief",
                                       f"package preparation failed: {exc}")
            self._trace(task.id, sender="chief", receiver="founder",
                        kind=KIND_BLOCK, parent=fail_id, objective=task.title,
                        status="blocked", detail={"reason": str(exc)})
            self.journal.record("escalate", f"PA pipeline failed for {task.id}: {exc}",
                                task_id=task.id)
            return None, None, TaskOutcome(
                task_id=task.id, status=out.status,
                outcome_class=OUTCOME_BLOCKED, detail=str(exc),
            )

        # On retries (or any re-entry), the new handoff chain links onto the
        # previous one so the full task timeline stays one connected graph.
        previous = self.ledger.handoffs_for(task.id)
        pa_parent = previous[-1].id if previous else None
        pa_id = self._trace(
            task.id, sender="chief", receiver="prompt_architect",
            kind=KIND_DISPATCH, parent=pa_parent, objective=task.title,
            prompt_id=package.prompt_id, prompt_version=package.prompt_version,
            context_refs=[cf.path for cf in package.context_files],
            model=self._model_name("prompt_architect"), status="completed",
            detail={"uncertainties": package.uncertainties,
                    "conflicts": package.conflicts},
        )
        return package, pa_id, None

    def _specialist_failure(self, task: TaskRecord, dispatch_id: str,
                            sp_result: RunResult) -> TaskOutcome:
        self._trace(
            task.id, sender=task.assignee_role, receiver="chief",
            kind=KIND_FAILURE, parent=dispatch_id, objective=task.title,
            status="failed",
            detail={"error": sp_result.failure_reason,
                    "steps_used": sp_result.steps_used,
                    "tools": self._tool_summary(sp_result.transcript_path)},
        )
        out = self.ledger.escalate(
            task.id, "chief",
            f"specialist run failed: {sp_result.status} {sp_result.failure_reason}",
        )
        self._trace(task.id, sender="chief", receiver="founder",
                    kind=KIND_BLOCK, parent=None, objective=task.title,
                    status="blocked", detail={"reason": sp_result.failure_reason})
        self.journal.record("escalate", f"specialist failed on {task.id}",
                            task_id=task.id)
        return TaskOutcome(task_id=task.id, status=out.status,
                           outcome_class=OUTCOME_BLOCKED,
                           detail=sp_result.failure_reason)

    def _review_verdict(self, task: TaskRecord, review: RunResult,
                        gates: GatesReport, dispatch_id: str) -> tuple[str, list[str], str]:
        verdict = "ISSUES"
        findings: list[str] = []
        if review.status == COMPLETED and review.report is not None:
            verdict = str(review.report.artifacts.get("verdict", "ISSUES"))
            raw_findings = review.report.artifacts.get("findings", [])
            findings = ([str(f) for f in raw_findings]
                        if isinstance(raw_findings, list) else [])
        actually_verified = gates.passed and verdict == "CLEAN"
        verdict_id = self._trace(
            task.id, sender=self.reviewer_role, receiver="chief",
            kind=KIND_VERDICT, parent=dispatch_id, objective=task.title,
            model=self._model_name(self.reviewer_role),
            status="completed" if review.status == COMPLETED else "failed",
            verified=actually_verified,
            detail={"verdict": verdict, "findings": findings,
                    "gates_passed": gates.passed,
                    "tools": self._tool_summary(review.transcript_path)},
        )
        return verdict, findings, verdict_id

    def _attempt(self, task: TaskRecord, package: TaskPackage, last_id: str,
                 revision: int) -> _Attempt:
        """One dispatch -> implementation -> gates -> independent review cycle."""
        dispatch_id = self._trace(
            task.id, sender="chief", receiver=task.assignee_role,
            kind=KIND_DISPATCH, parent=last_id, objective=task.title,
            prompt_id=package.prompt_id, prompt_version=package.prompt_version,
            context_refs=[cf.path for cf in package.context_files],
            model=self._model_name(task.assignee_role), status="pending",
            detail={"revision": revision},
        )
        sp_result = self._run_specialist(
            task, package, f"sp-{task.id}-{revision}-{_tag()}"
        )
        if sp_result.status != COMPLETED:
            return _Attempt(outcome=self._specialist_failure(task, dispatch_id, sp_result))
        if sp_result.report is None:
            raise RuntimeError("specialist completed without a report")
        # text output alone is never 'verified': the report handoff stays
        # verified=False until gates and the independent review pass.
        report_id = self._trace(
            task.id, sender=task.assignee_role, receiver="chief",
            kind=KIND_REPORT, parent=dispatch_id, objective=task.title,
            status="completed", verified=False,
            detail={
                "summary": sp_result.report.summary,
                "files_changed": sp_result.report.files_changed,
                "tests_run": sp_result.report.tests_run,
                "steps_used": sp_result.steps_used,
                "tools": self._tool_summary(sp_result.transcript_path),
            },
        )
        self.ledger.submit_for_review(
            task.id, task.assignee_role, sp_result.report.summary
        )
        gate_ws = sorted({
            cf.path.split(":", 1)[0] for cf in package.context_files if ":" in cf.path
        }) or list(self.config.gates)
        specs = [g for name, ws_gates in self.config.gates.items()
                 if name in gate_ws for g in ws_gates]
        gates = run_gates_for_task(self.ledger, task.id, specs, self.config.workspaces)
        gates_id = self._trace(
            task.id, sender="chief", receiver="gate_runner",
            kind=KIND_GATES, parent=report_id, objective=task.title,
            verified=gates.passed,
            detail={"summary": gates.summary()},
        )
        review = self._run_reviewer(
            task, sp_result, gates, f"rv-{task.id}-{revision}-{_tag()}"
        )
        rv_dispatch_id = self._trace(
            task.id, sender="chief", receiver=self.reviewer_role,
            kind=KIND_DISPATCH, parent=gates_id, objective=task.title,
            context_refs=list(sp_result.report.files_changed),
            model=self._model_name(self.reviewer_role), status="pending",
            detail={"stage": "independent_review"},
        )
        verdict, findings, verdict_id = self._review_verdict(
            task, review, gates, rv_dispatch_id
        )
        if gates.passed and verdict == "CLEAN":
            return _Attempt(outcome=self._approve_attempt(task, package, revision))
        self.ledger.back_to_review(task.id, "chief", "gates or review not clean")
        combined = "; ".join(findings) or gates.summary()
        return _Attempt(feedback=combined, parent_id=verdict_id)

    def _approve_attempt(self, task: TaskRecord, package: TaskPackage,
                         revision: int) -> TaskOutcome:
        if task.human_gate:
            self.journal.record(
                "human_gate", f"{task.id} passed gates+review; awaiting founder",
                task_id=task.id,
            )
            self.registry.record_outcome(
                package.prompt_id, package.prompt_version,
                f"chief-{task.id}", OUTCOME_BLOCKED,
            )
            return TaskOutcome(
                task_id=task.id, status=self.ledger.status_of(task.id),
                outcome_class=OUTCOME_BLOCKED,
                detail="human-gated: awaiting founder approval",
            )
        self.ledger.approve(task.id, "chief", "gates green, review clean")
        outcome_class = (
            APPROVED_FIRST_PASS if revision == 0
            else (REVISED_ONCE if revision == 1 else REVISED_TWICE)
        )
        self.registry.record_outcome(
            package.prompt_id, package.prompt_version,
            f"chief-{task.id}", outcome_class,
        )
        self.journal.record(
            "approve", f"{task.id} APPROVED after {revision} revisions "
            f"(gates green, review clean)", task_id=task.id,
        )
        return TaskOutcome(task_id=task.id, status=APPROVED,
                           outcome_class=outcome_class)

    def _apply_revision(self, task: TaskRecord, package: TaskPackage,
                        attempt: _Attempt) -> TaskOutcome | None:
        changed = self.ledger.request_changes(task.id, "chief", attempt.feedback)
        retry_id = self._trace(
            task.id, sender="chief", receiver=task.assignee_role,
            kind=KIND_RETRY, parent=attempt.parent_id,
            objective=task.title, status="completed",
            detail={"feedback": attempt.feedback,
                    "revision": changed.revisions_used},
        )
        if changed.status == BLOCKED_HUMAN:
            self._trace(task.id, sender="chief", receiver="founder",
                        kind=KIND_BLOCK, parent=retry_id,
                        objective=task.title, status="blocked",
                        detail={"reason": "revision budget exhausted"})
            self.registry.record_outcome(
                package.prompt_id, package.prompt_version,
                f"chief-{task.id}", OUTCOME_BLOCKED,
            )
            return TaskOutcome(task_id=task.id, status=BLOCKED_HUMAN,
                               outcome_class=OUTCOME_BLOCKED,
                               detail="revision budget exhausted")
        self.ledger.start_revision(task.id, task.assignee_role, attempt.feedback)
        self.journal.record(
            "revision", f"{task.id} revision {changed.revisions_used}: "
            f"{attempt.feedback}", task_id=task.id,
        )
        attempt.parent_id = retry_id
        return None

    def execute_task(self, task_id: str) -> TaskOutcome:
        task, skipped = self._ensure_running(task_id)
        if skipped is not None:
            return skipped
        assert task is not None
        package, pa_id, failure = self._prepare_task(task)
        if failure is not None:
            return failure
        assert package is not None and pa_id is not None
        last_id = pa_id
        revision = 0
        while True:
            attempt = self._attempt(task, package, last_id, revision)
            if attempt.outcome is not None:
                return attempt.outcome
            blocked = self._apply_revision(task, package, attempt)
            if blocked is not None:
                return blocked
            revision += 1
            last_id = attempt.parent_id

    # --- roadmap session ----------------------------------------------------------

    def sync_roadmap(self, roadmap: Roadmap) -> list[str]:
        created: list[str] = []
        for spec in roadmap.tasks:
            try:
                self.ledger.get(spec.id)
            except LedgerError:
                self.ledger.create(
                    spec.id, spec.title, spec.role, spec.depends,
                    spec.human_gate, spec.scope, spec.exclusions, spec.acceptance,
                )
                created.append(spec.id)
        return created

    def run_roadmap(self, roadmap: Roadmap, max_tasks: int = 10,
                    recover: bool = True) -> list[TaskOutcome]:
        if recover:
            from forge.recovery import plan_recovery

            plan = plan_recovery(
                self.ledger, self.config, self.config.limits.max_retries_per_run
            )
            if plan.interrupted:
                self.journal.record(
                    "recovery",
                    "recovered after interruption: "
                    + ", ".join(f"{i.task_id}({i.action})" for i in plan.interrupted),
                )
            for ws_name, files in plan.uncommitted.items():
                self.journal.record(
                    "uncommitted_changes",
                    f"workspace {ws_name} has uncommitted changes; "
                    "reported, not reverted: " + ", ".join(files[:5]),
                    reversible=False,
                )
        self.sync_roadmap(roadmap)
        outcomes: list[TaskOutcome] = []
        executed = 0
        for spec in roadmap.tasks:
            if executed >= max_tasks:
                break
            status = self.ledger.status_of(spec.id)
            if status in (APPROVED, BLOCKED_HUMAN):
                continue
            unmet = [d for d in spec.depends if self.ledger.status_of(d) != APPROVED]
            if unmet:
                outcomes.append(TaskOutcome(task_id=spec.id, status=status,
                                            detail=f"waiting on {unmet}"))
                continue
            outcomes.append(self.execute_task(spec.id))
            executed += 1
        return outcomes

    # --- reporting -------------------------------------------------------------------

    def daily_report(self) -> str:
        counts: dict[str, int] = {}
        for t in self.ledger.list_tasks():
            counts[t.status] = counts.get(t.status, 0) + 1
        approved = [t.id for t in self.ledger.list_tasks(APPROVED)]
        blocked = [t.id for t in self.ledger.list_tasks(BLOCKED_HUMAN)]
        lines = [
            f"VISHWAKARMA daily report — {_now()[:10]}",
            f"Approved ({len(approved)}): {', '.join(approved) or '-'}",
            f"Blocked-human ({len(blocked)}): {', '.join(blocked) or '-'}",
            f"Status counts: {json.dumps(counts, sort_keys=True)}",
            "Recent decisions:",
        ]
        for d in self.journal.recent(10):
            lines.append(f"- [{d['kind']}] {d['task_id'] or '-'}: {d['summary']}")
        return "\n".join(lines)
