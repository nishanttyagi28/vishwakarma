"""Forge CLI — the human and Chief Engineer interface to the ledger and gates.

Every lifecycle command goes through the ledger's enforced state machine, so
nothing the CLI does can bypass governance. Actor defaults: lifecycle decisions
are ``chief``; approvals default to ``chief`` and must be passed explicitly as
``--actor founder`` for human-gated tasks. Approval additionally requires
recorded passing gates and a CLEAN independent review recorded with
``task review``.

Usage examples:
    forge task create --id T-1 --title "..." --role backend
    forge task promote T-1
    forge task claim T-1 --run-id run-1
    forge task submit T-1 --actor backend --summary "..."
    forge gate run T-1
    forge task review T-1 --reviewer qa --verdict CLEAN
    forge task approve T-1 --actor chief --note "gates green"
    forge status
"""

import argparse
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from forge.config import ForgeConfig, load_config
from forge.gates import run_gates_for_task
from forge.ledger import (
    APPROVED,
    BACKLOG,
    BLOCKED_HUMAN,
    CANCELLED,
    CHANGES_REQUESTED,
    FOUNDER,
    IN_PROGRESS,
    KIND_VERDICT,
    READY,
    REVIEW,
    TESTING,
    TaskLedger,
)
from forge.model.base import ModelBackend
from forge.permissions import PermissionChecker

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "forge.yaml"
DEFAULT_DB = REPO_ROOT / "forge.sqlite3"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_GATES_FAILED = 3


@dataclass
class CliResult:
    """Output plus the process exit code the CLI should use."""

    output: str
    exit_code: int = EXIT_OK


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


def build_parser() -> argparse.ArgumentParser:
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

    p_review = tsub.add_parser("review")
    p_review.add_argument("task_id")
    p_review.add_argument("--reviewer", required=True)
    p_review.add_argument("--verdict", choices=["CLEAN", "ISSUES"], required=True)
    p_review.add_argument("--findings", nargs="*", default=[])

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
    p_crun.add_argument("--model", choices=["fake", "ollama", "openai"],
                        default="ollama",
                        help="model backend; 'fake' is scripted/no-network, "
                             "'openai' uses FORGE_OPENAI_* env configuration")
    csub.add_parser("report")
    return parser


# --- per-command handlers ----------------------------------------------------


def _task_create(args: argparse.Namespace, ledger: TaskLedger) -> list[str]:
    out: list[str] = []
    ledger.create(
        args.id, args.title, args.role, args.depends,
        args.human_gate, args.scope, args.exclusions, args.acceptance,
    )
    _print(out, f"created {args.id} in BACKLOG")
    return out


def _task_read(args: argparse.Namespace, ledger: TaskLedger) -> list[str]:
    out: list[str] = []
    if args.tcmd == "list":
        tasks = ledger.list_tasks(args.status)
        for t in tasks:
            _print(out, f"{t.id} [{t.status}] {t.assignee_role} - {t.title}")
        if not tasks:
            _print(out, "(no tasks)")
    elif args.tcmd == "show":
        _print(out, _task_show(ledger, args.task_id))
    else:  # history
        for tr in ledger.history(args.task_id):
            frm = tr.from_status or "create"
            _print(out, f"{tr.at} {frm} -> {tr.to_status} by {tr.actor}: {tr.reason}")
    return out


def _task_lifecycle(args: argparse.Namespace, ledger: TaskLedger) -> list[str]:
    out: list[str] = []
    if args.tcmd == "promote":
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
    else:  # unblock
        record = ledger.unblock(args.task_id, args.actor, args.reason)
        _print(out, f"{record.id} -> READY (unblocked for retry)")
    return out


def _task_review(args: argparse.Namespace, ledger: TaskLedger) -> list[str]:
    out: list[str] = []
    task_record = ledger.get(args.task_id)
    if args.reviewer == task_record.assignee_role:
        raise ValueError("reviewer must be independent of the implementer")
    gate_outcomes = ledger.latest_gate_outcomes(args.task_id)
    gates_green = bool(gate_outcomes) and all(gate_outcomes.values())
    hid = ledger.record_handoff(
        args.task_id, sender=args.reviewer, receiver="chief",
        kind=KIND_VERDICT, status="completed",
        verified=args.verdict == "CLEAN" and gates_green,
        detail={"verdict": args.verdict, "findings": args.findings,
                "gates_passed": gates_green},
    )
    _print(out, f"review {hid} recorded: {args.verdict} by {args.reviewer}")
    return out


def _task_approve(args: argparse.Namespace, ledger: TaskLedger) -> list[str]:
    out: list[str] = []
    if args.actor not in ("chief", FOUNDER):
        raise ValueError("approve actor must be chief or founder")
    record = ledger.approve(args.task_id, args.actor, args.note)
    _print(out, f"{record.id} -> APPROVED by {args.actor}")
    return out


def _handle_task(args: argparse.Namespace, ledger: TaskLedger) -> list[str]:
    if args.tcmd == "create":
        return _task_create(args, ledger)
    if args.tcmd in ("list", "show", "history"):
        return _task_read(args, ledger)
    if args.tcmd == "review":
        return _task_review(args, ledger)
    if args.tcmd == "approve":
        return _task_approve(args, ledger)
    return _task_lifecycle(args, ledger)


def _handle_gate(args: argparse.Namespace, ledger: TaskLedger,
                 config: ForgeConfig) -> CliResult:
    out: list[str] = []
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
    report = run_gates_for_task(ledger, args.task_id, specs, workspaces, args.timeout)
    _print(out, report.summary())
    _print(out, "GATES PASSED" if report.passed else "GATES FAILED")
    exit_code = EXIT_OK if report.passed else EXIT_GATES_FAILED
    return CliResult("\n".join(out), exit_code)


def _handle_recover(args: argparse.Namespace, ledger: TaskLedger,
                    config: ForgeConfig) -> list[str]:
    from forge.recovery import plan_recovery, revert_files

    out: list[str] = []
    plan = plan_recovery(ledger, config, config.limits.max_retries_per_run)
    for item in plan.interrupted:
        _print(out, f"{item.task_id}: {item.action} ({item.detail})")
    for ws_name, files in plan.uncommitted.items():
        _print(out, f"uncommitted in {ws_name}: {', '.join(files)}")
        if args.revert:
            _print(out, revert_files(Path(config.workspaces[ws_name]), files))
    if not plan.interrupted and not plan.uncommitted:
        _print(out, "nothing to recover")
    return out


def _handle_status(ledger: TaskLedger) -> list[str]:
    out: list[str] = []
    counts: dict[str, int] = {}
    for t in ledger.list_tasks():
        counts[t.status] = counts.get(t.status, 0) + 1
    for state in (BACKLOG, READY, IN_PROGRESS, REVIEW, TESTING,
                  CHANGES_REQUESTED, APPROVED, BLOCKED_HUMAN, CANCELLED):
        _print(out, f"{state}: {counts.get(state, 0)}")
    return out


def _handle_trace(args: argparse.Namespace, ledger: TaskLedger) -> list[str]:
    out: list[str] = []
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
        for u in ledger.usage_for_task(args.task_id):
            _print(out,
                   f"  usage: {u.at} {u.role} model={u.model} "
                   f"prompt={u.prompt_tokens} completion={u.completion_tokens} "
                   f"total={u.total_tokens} cost_usd={u.cost_usd:.6f} "
                   f"mission={u.mission_id or '-'}")
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
    return out


def _handle_ui(args: argparse.Namespace, ledger: TaskLedger) -> list[str]:
    from forge.webui import DashboardServer, build_dashboard_html

    out: list[str] = []
    if args.print:
        _print(out, build_dashboard_html(ledger))
        return out
    server = DashboardServer(ledger, port=args.port)
    _print(out, f"serving http://127.0.0.1:{server.port}/ (Ctrl+C stops)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
    return out


def _build_models(grants: Mapping[str, object], kind: str,
                  config: ForgeConfig) -> dict[str, ModelBackend]:
    from forge.model.fake import FakeModel
    from forge.model.ollama_backend import OllamaBackend
    from forge.model.openai_backend import OpenAICompatibleBackend

    models: dict[str, ModelBackend]
    if kind == "fake":
        models = {role: FakeModel([]) for role in grants}
    else:
        backend: ModelBackend = (
            OpenAICompatibleBackend.from_env() if kind == "openai"
            else OllamaBackend(
                model=config.model.default,
                timeout_s=float(config.limits.max_steps_per_run * 60),
            )
        )
        models = {role: backend for role in grants}
    return models


def _handle_chief(args: argparse.Namespace, ledger: TaskLedger,
                  config: ForgeConfig, config_path: Path) -> list[str]:
    from forge.chief import ChiefEngineer
    from forge.journal import DecisionJournal
    from forge.permissions import load_roles
    from forge.prompting import PromptRegistry

    out: list[str] = []
    grants = load_roles(REPO_ROOT / "forge" / "roles.yaml")
    checker = PermissionChecker(grants, config.workspaces)
    registry = PromptRegistry(
        config_path.parent / "forge-prompts.sqlite3",
        max_revisions=config.limits.max_revisions_per_task,
    )
    journal = DecisionJournal(config_path.parent / "decisions.sqlite3")
    try:
        models = _build_models(grants, getattr(args, "model", "ollama"), config)
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
        registry.close()
        journal.close()
    return out


def execute_result(argv: list[str], config_path: Path, db_path: Path) -> CliResult:
    """Run one CLI invocation; return output and the intended exit code."""
    args = build_parser().parse_args(argv)
    config = load_config(config_path)
    ledger = TaskLedger(db_path, max_revisions=config.limits.max_revisions_per_task)
    try:
        if args.cmd == "task":
            return CliResult("\n".join(_handle_task(args, ledger)))
        if args.cmd == "gate":
            return _handle_gate(args, ledger, config)
        if args.cmd == "recover":
            return CliResult("\n".join(_handle_recover(args, ledger, config)))
        if args.cmd == "status":
            return CliResult("\n".join(_handle_status(ledger)))
        if args.cmd == "trace":
            return CliResult("\n".join(_handle_trace(args, ledger)))
        if args.cmd == "ui":
            return CliResult("\n".join(_handle_ui(args, ledger)))
        return CliResult("\n".join(_handle_chief(args, ledger, config, config_path)))
    finally:
        ledger.close()


def execute(argv: list[str], config_path: Path, db_path: Path) -> str:
    """Run one CLI invocation against the given config and ledger. Returns output text."""
    return execute_result(argv, config_path, db_path).output


def main() -> None:
    import os

    config_path = Path(os.environ.get("FORGE_CONFIG", str(DEFAULT_CONFIG)))
    db_path = Path(os.environ.get("FORGE_DB", str(DEFAULT_DB)))
    try:
        result = execute_result(sys.argv[1:], config_path, db_path)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE) from exc
    except Exception as exc:  # ledger and gate errors surface with exit code 1
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_ERROR) from exc
    print(result.output)
    if result.exit_code:
        raise SystemExit(result.exit_code)


if __name__ == "__main__":
    main()
