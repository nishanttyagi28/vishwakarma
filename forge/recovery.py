"""Recovery: interrupted-run detection, safe resumption, retry budgeting.

At session start, any IN_PROGRESS task is interrupted by definition (the
worker policy is single-task sequential). Recovery returns such tasks to READY
while preserving the retry budget; exhausting it escalates to BLOCKED_HUMAN.

Uncommitted workspace changes are detected and reported but NEVER reverted
automatically - overwriting work is a human decision, executed only through
``revert_files`` which the CLI exposes behind an explicit flag.
"""

import subprocess
from dataclasses import field
from pathlib import Path

from pydantic import BaseModel

from forge.config import ForgeConfig
from forge.ledger import BLOCKED_HUMAN, IN_PROGRESS, TaskLedger


class InterruptedTask(BaseModel):
    task_id: str
    action: str  # "recovered" | "escalated"
    detail: str = ""


class RecoveryReport(BaseModel):
    interrupted: list[InterruptedTask] = field(default_factory=list)
    uncommitted: dict[str, list[str]] = field(default_factory=dict)


def git_porcelain(root: Path) -> list[str]:
    """Trusted mechanism: list uncommitted changes in a git workspace."""
    git_dir = root / ".git"
    if not git_dir.exists():
        return []
    proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        return []
    return [line[3:].strip() for line in proc.stdout.splitlines() if line.strip()]


def plan_recovery(ledger: TaskLedger, config: ForgeConfig, max_retries: int) -> RecoveryReport:
    """Reset interrupted tasks within retry budget; report uncommitted work."""
    report = RecoveryReport()
    for task in ledger.list_tasks(IN_PROGRESS):
        recovered = ledger.recover_task(task.id, max_retries)
        action = "recovered" if recovered.status != BLOCKED_HUMAN else "escalated"
        report.interrupted.append(InterruptedTask(
            task_id=task.id, action=action, detail=recovered.claimed_run or "",
        ))
    for name, root in config.workspaces.items():
        files = git_porcelain(Path(root))
        if files:
            report.uncommitted[name] = files
    return report


def revert_files(root: Path, files: list[str]) -> str:
    """Human-invoked only: restore specific modified files from git HEAD."""
    proc = subprocess.run(
        ["git", "checkout", "--", *files],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    return f"exit={proc.returncode}\n{proc.stdout}{proc.stderr}".strip()
