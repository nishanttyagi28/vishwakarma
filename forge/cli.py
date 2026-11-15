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
        elif args.cmd == "status":
            counts: dict[str, int] = {}
            for t in ledger.list_tasks():
                counts[t.status] = counts.get(t.status, 0) + 1
            for state in (BACKLOG, READY, IN_PROGRESS, REVIEW, TESTING,
                          CHANGES_REQUESTED, APPROVED, BLOCKED_HUMAN, CANCELLED):
                _print(out, f"{state}: {counts.get(state, 0)}")
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
