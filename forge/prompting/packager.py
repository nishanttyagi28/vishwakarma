"""Packager: deterministic assembly of TaskPackages with PA curation in the middle.

Pipeline:
1. ``gather_candidates`` — deterministic keyword retrieval over the workspaces.
2. PA agent run (bounded, permission-checked, FakeModel in tests) — curates
   which candidates matter, writes instructions, flags uncertainty.
3. ``assemble`` — merges the curation with code-owned facts (task record,
   role grants copied verbatim, configured gates) and validates everything.
"""

from pathlib import Path

from pydantic import BaseModel, Field

from forge.config import ForgeConfig
from forge.ledger import TaskRecord
from forge.permissions import PermissionChecker
from forge.prompting.package import (
    CHARS_PER_TOKEN,
    ContextFile,
    PackageValidationError,
    TaskPackage,
    estimate_tokens,
    expected_grants,
    package_hash,
    validate_package,
)
from forge.prompting.retrieval import search_workspace
from forge.prompting.secrets import is_sensitive_path


class Curation(BaseModel):
    context_selected: list[str] = Field(default_factory=list)
    instructions: str = ""
    uncertainties: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)


def gather_candidates(task: TaskRecord, config: ForgeConfig) -> list[str]:
    """Workspace-keyword retrieval; deterministic, secret files excluded."""
    keywords = [w for w in task.title.lower().replace(",", " ").split() if len(w) > 2]
    candidates: list[str] = []
    for ws_name, root in config.workspaces.items():
        for rel, _score in search_workspace(Path(root), keywords):
            if is_sensitive_path(Path(root) / rel):
                continue
            candidates.append(f"{ws_name}:{rel}")
    return candidates


def pa_brief(task: TaskRecord, candidates: list[str]) -> str:
    return (
        f"Task {task.id} ({task.assignee_role}): {task.title}\n"
        f"Objective: {task.scope or task.title}\n"
        f"Exclusions: {task.exclusions or 'none'}\n"
        f"Acceptance: {task.acceptance or 'as per task record'}\n\n"
        "Candidate context files (deterministic retrieval):\n"
        + "\n".join(f"- {c}" for c in candidates or ["(none found)"])
        + "\n\nSelect the minimum sufficient context. Reply per protocol; in your "
        "final report use artifacts: {context_selected: [paths], instructions: str, "
        "uncertainties: [..], conflicts: [..]}."
    )


def assemble(
    task: TaskRecord,
    curation: Curation,
    checker: PermissionChecker,
    config: ForgeConfig,
    package_id: str,
    prompt_id: str = "",
    prompt_version: int = 0,
    token_cap: int = 12000,
) -> TaskPackage:
    """Build and validate the package. Deterministic facts come from code."""
    gate_commands = [g.command for ws in config.gates.values() for g in ws]
    try:
        grants_snapshot = expected_grants(checker, task.assignee_role)
    except Exception as exc:
        raise PackageValidationError([f"unknown target role: {exc}"]) from None
    context_files = [
        ContextFile(path=spec, reason="PA-curated context") for spec in curation.context_selected
    ]
    package = TaskPackage(
        package_id=package_id,
        task_id=task.id,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        target_role=task.assignee_role,
        task_objective=task.title,
        context_files=context_files,
        architecture_constraints=[],
        dependencies=list(task.depends_on),
        prerequisites=[],
        in_scope=[task.scope] if task.scope else [task.title],
        out_of_scope=[task.exclusions] if task.exclusions else [],
        tool_grants=grants_snapshot,
        expected_output="working implementation + passing tests + updated docs",
        acceptance_criteria=[task.acceptance] if task.acceptance else ["task verified by gates"],
        verification_commands=gate_commands,
        failure_conditions=["tests fail after revision budget", "permission denial repeats"],
        escalation_conditions=[
            "requirement is ambiguous", "scope must change", "security risk found",
            "human-gated action required",
        ],
        instructions=curation.instructions,
        uncertainties=curation.uncertainties,
        conflicts=curation.conflicts,
    )
    package.estimated_tokens = estimate_tokens(
        package.model_dump_json() + _excerpts_total(package, checker)
    )
    validate_package(package, task, checker, config, token_cap)
    return package


def _excerpts_total(package: TaskPackage, checker: PermissionChecker) -> str:
    total = 0
    for cf in package.context_files:
        try:
            path = checker.check_fs(package.target_role, "read", cf.path)
            total += min(len(path.read_text(encoding="utf-8", errors="replace")), 1500)
        except Exception:
            continue
    return " " * total


def rendered_token_estimate(package: TaskPackage, checker: PermissionChecker) -> int:
    from forge.prompting.package import render

    return len(render(package, checker)) // CHARS_PER_TOKEN


__all__ = [
    "Curation",
    "assemble",
    "gather_candidates",
    "package_hash",
    "pa_brief",
    "rendered_token_estimate",
]
