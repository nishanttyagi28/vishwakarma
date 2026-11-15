"""Forge CLI — the human and Chief Engineer interface to the ledger and gates.

Every lifecycle command goes through the ledger's enforced state machine, so
nothing the CLI does can bypass governance. Actor defaults: lifecycle decisions
are ``chief``; approvals default to ``chief`` and must be passed explicitly as
``--actor founder`` for human-gated tasks.

Usage examples:
    forge task create --id T-1 --title "..." --role backend
    forge task promote T-1
    forge task claim T-1 --run-id run-1
    forge task submit T-1 --actor backend --summary "..."
    forge gate run T-1
    forge task approve T-1 --actor chief --note "gates green"
    forge status
"""

import argparse
import json
import sys
from pathlib import Path

from forge.config import load_config
from forge.gates import GatesReport, run_gates_for_task
from forge.ledger import (
    APPROVED,
    BACKLOG,
    BLOCKED_HUMAN,
    CANCELLED,
    CHANGES_REQUESTED,
    FOUNDER,
    IN_PROGRESS,
    READY,
    REVIEW,
    TESTING,
    TaskLedger,
)
from forge.permissions import PermissionChecker

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "forge.yaml"
DEFAULT_DB = REPO_ROOT / "forge.sqlite3"


def _print(out: list[str], message: str) -> None:
    out.append(message)


def _task_show(ledger: TaskLedger, task_id: str) -> str:
    task = ledger.get(task_id)
    deps = ", ".join(task.depends_on) or "-"
    gate_note = " (human gate)" if task.human_gate else ""
    return (
        f"{task.id} [{task.status}]{gate_note} role={task.assignee_role} "
        f"deps={deps} revisions={task.revisions_used}\n"
        f"  title: {task.title}\n  scope: {task.scope}\n"
        f"  exclusions: {task.exclusions}\n  acceptance: {task.acceptance}"
    )


def execute(argv: list[str], config_path: Path, db_path: Path) -> str:
    """Run one CLI invocation against the given config and ledger. Returns output text."""
    out: list[str] = []
    parser = argparse.ArgumentParser(prog="forge")
    sub = parser.add_subparsers(dest="cmd", required=True)

    task = sub.add_parser("task")
    tsub = task.add_subparsers(dest="tcmd", required=True)

    p_create = tsub.add_parser("create")
    p_create.add_argument("--id", required=True)
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--role", required=True)
    p_create.add_argument("--depends", nargs="*", default=[])
    p_create.add_argument("--human-gate", action="store_true")
    p_create.add_argument("--scope", default="")
    p_create.add_argument("--exclusions", default="")
    p_create.add_argument("--acceptance", default="")

    for name in ("promote", "show", "history"):
        p = tsub.add_parser(name)
        p.add_argument("task_id")

    p_claim = tsub.add_parser("claim")
    p_claim.add_argument("task_id")
    p_claim.add_argument("--run-id", required=True)

    p_gates = tsub.add_parser("gates")
    p_gates.add_argument("task_id")

    p_submit = tsub.add_parser("submit")
    p_submit.add_argument("task_id")
    p_submit.add_argument("--actor", required=True)
    p_submit.add_argument("--summary", required=True)

    p_changes = tsub.add_parser("changes")
    p_changes.add_argument("task_id")
    p_changes.add_argument("--actor", required=True)
    p_changes.add_argument("--findings", required=True)

    p_revise = tsub.add_parser("revise")
    p_revise.add_argument("task_id")
    p_revise.add_argument("--actor", required=True)
    p_revise.add_argument("--plan", required=True)

    p_escalate = tsub.add_parser("escalate")
    p_escalate.add_argument("task_id")
    p_escalate.add_argument("--actor", required=True)
    p_escalate.add_argument("--reason", required=True)

    p_cancel = tsub.add_parser("cancel")
    p_cancel.add_argument("task_id")
    p_cancel.add_argument("--actor", required=True)
    p_cancel.add_argument("--reason", required=True)

    p_unblock = tsub.add_parser("unblock")
    p_unblock.add_argument("task_id")
    p_unblock.add_argument("--actor", required=True)
    p_unblock.add_argument("--reason", required=True)

    p_approve = tsub.add_parser("approve")
    p_approve.add_argument("task_id")
    p_approve.add_argument("--actor", required=True)
    p_approve.add_argument("--note", default="")

    t_list = tsub.add_parser("list")
    t_list.add_argument("--status", default=None)

    gate = sub.add_parser("gate")
    gsub = gate.add_subparsers(dest="gcmd", required=True)
    p_grun = gsub.add_parser("run")
    p_grun.add_argument("task_id")
    p_grun.add_argument("--workspace", default=None)
    p_grun.add_argument("--timeout", type=float, default=900.0)

    sub.add_parser("status")
    trace = sub.add_parser("trace")
    tsub2 = trace.add_subparsers(dest="trcmd", required=True)
    p_tshow = tsub2.add_parser("show")
    p_tshow.add_argument("task_id")
    tsub2.add_parser("failed")

    p_recover = sub.add_parser("recover")
    p_recover.add_argument("--revert", action="store_true",
                           help="human-invoked: restore uncommitted files from git HEAD")
    p_ui = sub.add_parser("ui", help="read-only local dashboard (127.0.0.1)")
    p_ui.add_argument("--port", type=int, default=8765)
    p_ui.add_argument("--print", action="store_true",
                      help="print the dashboard HTML instead of serving")

    chief = sub.add_parser("chief")
    csub = chief.add_subparsers(dest="ccmd", required=True)
    p_crun = csub.add_parser("run")
    p_crun.add_argument("--task", required=True)
    csub.add_parser("report")

    args = parser.parse_args(argv)
    config = load_config(config_path)
    ledger = TaskLedger(db_path, max_revisions=config.limits.max_revisions_per_task)
    try:
        if args.cmd == "task":
            if args.tcmd == "create":
                ledger.create(
                    args.id, args.title, args.role, args.depends,
                    args.human_gate, args.scope, args.exclusions, args.acceptance,
                )
                _print(out, f"created {args.id} in BACKLOG")
            elif args.tcmd == "list":
                tasks = ledger.list_tasks(args.status)
                for t in tasks:
                    _print(out, f"{t.id} [{t.status}] {t.assignee_role} - {t.title}")
                if not tasks:
                    _print(out, "(no tasks)")
            elif args.tcmd == "show":
                _print(out, _task_show(ledger, args.task_id))
            elif args.tcmd == "history":
                for tr in ledger.history(args.task_id):
                    frm = tr.from_status or "create"
                    _print(out, f"{tr.at} {frm} -> {tr.to_status} by {tr.actor}: {tr.reason}")
            elif args.tcmd == "promote":
                record = ledger.promote(args.task_id)
                _print(out, f"{record.id} -> READY")
            elif args.tcmd == "claim":
                record = ledger.claim(args.task_id, args.run_id)
                _print(out, f"{record.id} -> IN_PROGRESS (run {args.run_id})")
            elif args.tcmd == "submit":
                record = ledger.submit_for_review(args.task_id, args.actor, args.summary)
                _print(out, f"{record.id} -> REVIEW")
            elif args.tcmd == "gates":
                record = ledger.request_gates(args.task_id)
                _print(out, f"{record.id} -> TESTING")
            elif args.tcmd == "changes":
                record = ledger.request_changes(args.task_id, args.actor, args.findings)
                _print(out, f"{record.id} -> {record.status}")
            elif args.tcmd == "revise":
                record = ledger.start_revision(args.task_id, args.actor, args.plan)
                _print(out, f"{record.id} -> IN_PROGRESS (revision)")
            elif args.tcmd == "escalate":
                record = ledger.escalate(args.task_id, args.actor, args.reason)
                _print(out, f"{record.id} -> BLOCKED_HUMAN")
            elif args.tcmd == "cancel":
                record = ledger.cancel(args.task_id, args.actor, args.reason)
                _print(out, f"{record.id} -> CANCELLED")
            elif args.tcmd == "unblock":
                record = ledger.unblock(args.task_id, args.actor, args.reason)
                _print(out, f"{record.id} -> READY (unblocked for retry)")
            elif args.tcmd == "approve":
                if args.actor not in ("chief", FOUNDER):
                    raise ValueError("approve actor must be chief or founder")
                record = ledger.approve(args.task_id, args.actor, args.note)
                _print(out, f"{record.id} -> APPROVED by {args.actor}")
        elif args.cmd == "gate":
            if args.gcmd == "run":
                if args.workspace:
                    specs = config.gates.get(args.workspace, [])
                    workspaces = {args.workspace: config.workspaces[args.workspace]}
                else:
                    specs, workspaces = [], {}
                    for ws_name in config.workspaces:
                        specs.extend(config.gates.get(ws_name, []))
                        workspaces.setdefault(ws_name, config.workspaces[ws_name])
                if not specs:
                    raise ValueError("no gates configured for the requested scope")
                report: GatesReport = run_gates_for_task(
                    ledger, args.task_id, specs, workspaces, args.timeout
                )
                _print(out, report.summary())
                _print(out, "GATES PASSED" if report.passed else "GATES FAILED")
        elif args.cmd == "recover":
            from forge.recovery import plan_recovery, revert_files

            plan = plan_recovery(
                ledger, config, config.limits.max_retries_per_run
            )
            for item in plan.interrupted:
                _print(out, f"{item.task_id}: {item.action} ({item.detail})")
            for ws_name, files in plan.uncommitted.items():
                _print(out, f"uncommitted in {ws_name}: {', '.join(files)}")
                if args.revert:
                    _print(out, revert_files(Path(config.workspaces[ws_name]), files))
            if not plan.interrupted and not plan.uncommitted:
                _print(out, "nothing to recover")
        elif args.cmd == "status":
            counts: dict[str, int] = {}
            for t in ledger.list_tasks():
                counts[t.status] = counts.get(t.status, 0) + 1
            for state in (BACKLOG, READY, IN_PROGRESS, REVIEW, TESTING,
                          CHANGES_REQUESTED, APPROVED, BLOCKED_HUMAN, CANCELLED):
                _print(out, f"{state}: {counts.get(state, 0)}")
        elif args.cmd == "trace":
            if args.trcmd == "show":
                for h in ledger.handoffs_for(args.task_id):
                    detail = ""
                    if h.detail:
                        detail = " " + json.dumps(h.detail, ensure_ascii=False)[:160]
                    _print(out,
                           f"[{h.seq}] {h.at} {h.id} {h.sender}->{h.receiver} "
                           f"{h.kind} status={h.status} verified={h.verified} "
                           f"model={h.model} prompt={h.prompt_id}v{h.prompt_version} "
                           f"refs={h.context_refs}{detail}")
                for tr in ledger.history(args.task_id):
                    _print(out, f"  state: {tr.at} {tr.from_status}->{tr.to_status} "
                                f"by {tr.actor}: {tr.reason}")
            else:
                hs = ledger.failed_handoffs()
                if not hs:
                    _print(out, "no failed or blocked handoffs")
                for h in hs:
                    _print(out,
                           f"[{h.seq}] {h.at} {h.task_id} {h.id} "
                           f"{h.sender}->{h.receiver} {h.kind} "
                           f"status={h.status} detail={h.detail}")
        elif args.cmd == "ui":
            from forge.webui import DashboardServer, build_dashboard_html
            if args.print:
                _print(out, build_dashboard_html(ledger))
            else:
                server = DashboardServer(ledger, port=args.port)
                _print(out, f"serving http://127.0.0.1:{server.port}/ (Ctrl+C stops)")
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
                finally:
                    server.close()
        elif args.cmd == "chief":
            from forge.chief import ChiefEngineer
            from forge.journal import DecisionJournal
            from forge.model.base import ModelBackend
            from forge.model.ollama_backend import OllamaBackend
            from forge.permissions import load_roles
            from forge.prompting import PromptRegistry

            grants = load_roles(config_path.parent / "forge" / "roles.yaml")
            checker = PermissionChecker(grants, config.workspaces)
            ledger = TaskLedger(db_path, max_revisions=config.limits.max_revisions_per_task)
            registry = PromptRegistry(
                config_path.parent / "forge-prompts.sqlite3",
                max_revisions=config.limits.max_revisions_per_task,
            )
            journal = DecisionJournal(config_path.parent / "decisions.sqlite3")
            try:
                backend_model = OllamaBackend(
                    model=config.model.default,
                    timeout_s=float(config.limits.max_steps_per_run * 60),
                )
                models: dict[str, ModelBackend] = {
                    role: backend_model for role in grants
                }
                chief_eng = ChiefEngineer(
                    config, ledger, registry, journal, checker, models,
                    transcript_dir=config_path.parent / "runs",
                    max_steps_per_run=config.limits.max_steps_per_run,
                )
                if args.ccmd == "run":
                    outcome = chief_eng.execute_task(args.task)
                    _print(out, f"{outcome.task_id} -> {outcome.status} "
                                f"[{outcome.outcome_class or '-'}] {outcome.detail}")
                else:
                    _print(out, chief_eng.daily_report())
            finally:
                ledger.close()
                registry.close()
                journal.close()
    finally:
        ledger.close()
    return "\n".join(out)


def main() -> None:
    try:
        text = execute(sys.argv[1:], DEFAULT_CONFIG, DEFAULT_DB)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except Exception as exc:  # ledger and gate errors surface with exit code 1
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(text)
    if "GATES FAILED" in text:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
