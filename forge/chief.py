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
    LedgerError,
    TaskLedger,
    TaskRecord,
)
from forge.model.base import ModelBackend
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
    pa_brief,
    render,
)
from forge.prompting import (
    BLOCKED_HUMAN as OUTCOME_BLOCKED,
)
from forge.prompting.package import TaskPackage, package_hash
from forge.runtime.agent import COMPLETED, AgentRuntime, RunResult
from forge.runtime.tools import ToolExecutor, ToolResult


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

    def prepare_package(self, task: TaskRecord) -> tuple[TaskPackage, RunResult]:
        candidates = gather_candidates(task, self.config)
        pa_role = "prompt_architect"
        grant = self.checker.grants[pa_role]
        named = {"repo.search": self._repo_search_tool()}
        tools = ToolExecutor(self.checker, pa_role, named_tools=named)
        agent = AgentRuntime(
            model=self.models[pa_role],
            tools=tools,
            role=pa_role,
            mission=grant.mission,
            max_steps=self.max_steps,
            transcript_dir=self.transcript_dir,
        )
        result = agent.run(f"pa-{task.id}-{_tag()}", pa_brief(task, candidates))
        if result.status != COMPLETED or result.report is None:
            raise RuntimeError(f"PA run failed: {result.status} {result.failure_reason}")
        artifacts = result.report.artifacts
        raw_ctx = artifacts.get("context_selected", [])
        unc = artifacts.get("uncertainties", [])
        conf = artifacts.get("conflicts", [])
        curation = Curation(
            context_selected=[str(p) for p in raw_ctx] if isinstance(raw_ctx, list) else [],
            instructions=str(artifacts.get("instructions", "")),
            uncertainties=[str(u) for u in unc] if isinstance(unc, list) else [],
            conflicts=[str(c) for c in conf] if isinstance(conf, list) else [],
        )
        prompt_id = f"p-{task.id}"
        package = assemble(
            task, curation, self.checker, self.config, f"pkg-{task.id}",
            prompt_id=prompt_id, prompt_version=0, token_cap=self.token_cap,
        )
        version = self._register_prompt(prompt_id, package, task)
        package.prompt_version = version
        validate_again = package.model_copy(update={"prompt_version": version})
        return validate_again, result

    def _register_prompt(self, prompt_id: str, package, task: TaskRecord) -> int:
        version = self.registry.register(
            prompt_id, task.assignee_role, "implementation", package_hash(package)
        )
        if version > 1:
            self.registry.supersede(prompt_id, version - 1, version)
        return version

    def _repo_search_tool(self):
        from forge.prompting.retrieval import search_workspace

        def _search(args: dict[str, object]) -> ToolResult:
            raw_keywords = args.get("keywords", [])
            keywords = [str(k) for k in raw_keywords] if isinstance(raw_keywords, list) else []
            lines = []
            for ws_name, root in self.config.workspaces.items():
                for rel, score in search_workspace(Path(root), keywords):
                    lines.append(f"{ws_name}:{rel} (score {score})")
            return ToolResult(True, "\n".join(lines) or "(no matches)")

        return _search

    # --- specialist and reviewer runs -----------------------------------------

    def _run_specialist(self, task: TaskRecord, context: str, run_id: str,
                        feedback: str = "") -> RunResult:
        role = task.assignee_role
        tools = ToolExecutor(self.checker, role)
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
        full_context = context + feedback_block
        return agent.run(run_id, full_context)

    def _run_reviewer(self, task: TaskRecord, package, specialist: RunResult,
                      gates: GatesReport, run_id: str) -> RunResult:
        role = self.reviewer_role
        tools = ToolExecutor(self.checker, role)
        agent = AgentRuntime(
            model=self.models[role],
            tools=tools,
            role=role,
            mission=self.checker.grants[role].mission,
            max_steps=self.max_steps,
            transcript_dir=self.transcript_dir,
        )
        context = (
            f"INDEPENDENT REVIEW of task {task.id}\n"
            f"Objective: {task.title}\n"
            f"Specialist report: "
            f"{specialist.report.model_dump_json() if specialist.report else 'none'}\n"
            f"Gate summary:\n{gates.summary()}\n"
            "Read the changed files with fs.read if needed. Reply per protocol; "
            "in your final report set artifacts: {verdict: CLEAN|ISSUES, findings: [...]}."
        )
        return agent.run(run_id, context)

    # --- core loop --------------------------------------------------------------

    def execute_task(self, task_id: str) -> TaskOutcome:
        task = self.ledger.get(task_id)
        if task.status == BACKLOG:
            try:
                self.ledger.promote(task_id)
            except LedgerError as exc:
                return TaskOutcome(task_id=task_id, status=self.ledger.status_of(task_id),
                                   detail=f"cannot promote: {exc}")
        if self.ledger.status_of(task_id) in (BLOCKED_HUMAN, APPROVED):
            return TaskOutcome(task_id=task_id, status=self.ledger.status_of(task_id),
                               detail="terminal state, skipped")
        if self.ledger.status_of(task_id) == BACKLOG:
            self.ledger.promote(task_id)
        if self.ledger.status_of(task_id) == BACKLOG:
            self.ledger.promote(task_id)
        if self.ledger.status_of(task_id) != IN_PROGRESS:
            self.ledger.claim(task_id, f"chief-{_tag()}")
        task = self.ledger.get(task_id)

        self.journal.record("dispatch", f"delegated {task_id} to {task.assignee_role} "
                            f"via PA-prepared package", task_id=task_id)

        try:
            package, _pa_result = self.prepare_package(task)
        except (RuntimeError, PackageValidationError) as exc:
            out = self.ledger.escalate(task_id, "chief", f"package preparation failed: {exc}")
            self.journal.record("escalate", f"PA pipeline failed for {task_id}: {exc}",
                                task_id=task_id)
            return TaskOutcome(task_id=task_id, status=out.status,
                               outcome_class=OUTCOME_BLOCKED, detail=str(exc))

        context = render(package, self.checker)
        revision = 0
        while True:
            sp_result = self._run_specialist(
                task, context, f"sp-{task_id}-{revision}-{_tag()}"
            )
            if sp_result.status != COMPLETED:
                out = self.ledger.escalate(
                    task_id, "chief",
                    f"specialist run failed: {sp_result.status} {sp_result.failure_reason}",
                )
                self.journal.record("escalate", f"specialist failed on {task_id}",
                                    task_id=task_id)
                return TaskOutcome(task_id=task_id, status=out.status,
                                   outcome_class=OUTCOME_BLOCKED,
                                   detail=sp_result.failure_reason)

            if sp_result.report is None:
                raise RuntimeError("specialist completed without a report")
            self.ledger.submit_for_review(
                task_id, task.assignee_role, sp_result.report.summary
            )
            specs = [g for ws in self.config.gates.values() for g in ws]
            gates = run_gates_for_task(
                self.ledger, task_id, specs, self.config.workspaces
            )
            review = self._run_reviewer(
                task, package, sp_result, gates, f"rv-{task_id}-{revision}-{_tag()}"
            )
            verdict = "ISSUES"
            findings: list[str] = []
            if review.status == COMPLETED and review.report is not None:
                verdict = str(review.report.artifacts.get("verdict", "ISSUES"))
                raw_findings = review.report.artifacts.get("findings", [])
                findings = ([str(f) for f in raw_findings]
                            if isinstance(raw_findings, list) else [])

            if gates.passed and verdict == "CLEAN":
                if task.human_gate:
                    self.journal.record(
                        "human_gate", f"{task_id} passed gates+review; awaiting founder",
                        task_id=task_id,
                    )
                    self.registry.record_outcome(
                        package.prompt_id, package.prompt_version,
                        f"chief-{task_id}", OUTCOME_BLOCKED,
                    )
                    return TaskOutcome(
                        task_id=task_id, status=self.ledger.status_of(task_id),
                        outcome_class=OUTCOME_BLOCKED,
                        detail="human-gated: awaiting founder approval",
                    )
                actor = "chief"
                self.ledger.approve(task_id, actor, "gates green, review clean")
                outcome_class = (
                    APPROVED_FIRST_PASS if revision == 0
                    else (REVISED_ONCE if revision == 1 else REVISED_TWICE)
                )
                self.registry.record_outcome(
                    package.prompt_id, package.prompt_version, f"chief-{task_id}",
                    outcome_class,
                )
                self.journal.record(
                    "approve", f"{task_id} APPROVED after {revision} revisions "
                    f"(gates green, review clean)", task_id=task_id,
                )
                return TaskOutcome(task_id=task_id, status=APPROVED,
                                   outcome_class=outcome_class)

            # not approved: bounded revision (the ledger escalates at budget)
            self.ledger.back_to_review(task_id, "chief", "gates or review not clean")
            combined = "; ".join(findings) or gates.summary()
            changed = self.ledger.request_changes(task_id, "chief", combined)
            if changed.status == BLOCKED_HUMAN:
                self.registry.record_outcome(
                    package.prompt_id, package.prompt_version,
                    f"chief-{task_id}", OUTCOME_BLOCKED,
                )
                return TaskOutcome(task_id=task_id, status=BLOCKED_HUMAN,
                                   outcome_class=OUTCOME_BLOCKED,
                                   detail="revision budget exhausted")
            revision += 1
            self.ledger.start_revision(task_id, task.assignee_role, combined)
            self.journal.record(
                "revision", f"{task_id} revision {revision}: {combined}",
                task_id=task_id,
            )

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

    def run_roadmap(self, roadmap: Roadmap, max_tasks: int = 10) -> list[TaskOutcome]:
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
