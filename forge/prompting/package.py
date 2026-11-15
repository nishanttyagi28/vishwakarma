"""TaskPackage schema and deterministic validation.

The TaskPackage is the contract between the Prompt Architect's judgement and
the specialists' execution. Judgement lives in the curation (which files, what
instructions); everything checkable is checked here in code:

- every context file must exist and be read-granted to the target role
- architecture-constraint references must resolve to real files
- out_of_scope must include the task record's exclusions (the PA may clarify,
  never enlarge scope)
- tool grants in the package must be byte-identical to the role's declared
  grants (the PA cannot alter permissions)
- verification commands must be present and must include the workspace's
  configured trusted gates
- the estimated token size must fit the model's context budget
"""

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, Field

from forge.config import ForgeConfig
from forge.ledger import TaskRecord
from forge.permissions import PermissionChecker
from forge.prompting.secrets import is_sensitive_path, redact_text

EXCERPT_CHARS = 1500
CHARS_PER_TOKEN = 4


class ContextFile(BaseModel):
    path: str  # "workspace:relative/path"
    reason: str = ""
    content_excerpt: str = ""


class TaskPackage(BaseModel):
    package_id: str
    task_id: str
    prompt_id: str = ""
    prompt_version: int = 0
    target_role: str
    task_objective: str
    context_files: list[ContextFile] = Field(default_factory=list)
    interfaces: list[str] = Field(default_factory=list)
    architecture_constraints: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    in_scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    tool_grants: dict[str, list[str]] = Field(default_factory=dict)
    expected_output: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    verification_commands: list[str] = Field(default_factory=list)
    failure_conditions: list[str] = Field(default_factory=list)
    escalation_conditions: list[str] = Field(default_factory=list)
    estimated_tokens: int = 0
    instructions: str = ""
    uncertainties: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)


class PackageValidationError(Exception):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def _grants_snapshot(grant) -> dict[str, list[str]]:
    return {
        "fs.read": list(grant.fs.read),
        "fs.write": list(grant.fs.write),
        "shell_allow": list(grant.shell_allow),
        "tools": list(grant.tools),
    }


def expected_grants(checker: PermissionChecker, role: str) -> dict[str, list[str]]:
    return _grants_snapshot(checker.grants[role])


def validate_package(
    package: TaskPackage,
    task: TaskRecord,
    checker: PermissionChecker,
    config: ForgeConfig,
    token_cap: int,
    gate_workspaces: list[str] | None = None,
) -> None:
    """Raise PackageValidationError listing every violated invariant."""
    errors: list[str] = []

    if not package.task_objective.strip():
        errors.append("task_objective is empty")
    if not package.acceptance_criteria:
        errors.append("acceptance_criteria is empty")
    if not package.verification_commands:
        errors.append("verification_commands is empty")
    if package.estimated_tokens > token_cap:
        errors.append(
            f"estimated tokens {package.estimated_tokens} exceed cap {token_cap}"
        )

    # context files: exist, inside a known workspace, readable by the role
    for cf in package.context_files:
        try:
            path = checker.check_fs(package.target_role, "read", cf.path)
        except Exception as exc:
            errors.append(f"context file {cf.path!r} not readable by role: {exc}")
            continue
        if not path.exists():
            errors.append(f"context file {cf.path!r} does not exist")

    # architecture constraints must resolve
    for ref in package.architecture_constraints:
        try:
            path = checker.check_fs(package.target_role, "read", ref)
            if not path.exists():
                errors.append(f"architecture constraint {ref!r} does not exist")
        except Exception as exc:
            errors.append(f"architecture constraint {ref!r} invalid: {exc}")

    # scope fence: task exclusions must appear in out_of_scope
    exclusion_text = task.exclusions.strip()
    if exclusion_text:
        joined = " | ".join(package.out_of_scope).lower()
        for clause in [c.strip().lower() for c in exclusion_text.split(";") if c.strip()]:
            if clause not in joined:
                errors.append(f"task exclusion {clause!r} missing from out_of_scope")

    # permissions are copied, never authored by the PA
    if package.tool_grants != expected_grants(checker, package.target_role):
        errors.append("tool_grants do not match the role's declared grants")

    # trusted gates must be part of the verification story (filtered by the
    # workspaces the task actually touches)
    relevant_gates = [
        (name, ws_gates) for name, ws_gates in config.gates.items()
        if gate_workspaces is None or name in gate_workspaces
    ]
    for _name, ws_gates in relevant_gates:
        for gate in ws_gates:
            if gate.command not in package.verification_commands:
                errors.append(f"configured gate {gate.name!r} missing from verification_commands")
                break
        else:
            continue
        break

    if errors:
        raise PackageValidationError(errors)


def estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def package_hash(package: TaskPackage) -> str:
    canonical = json.dumps(package.model_dump(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def excerpt_for(path: Path) -> str:
    if is_sensitive_path(path):
        return "(sensitive file excluded from context)"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(unreadable file)"
    return redact_text(text[:EXCERPT_CHARS])


def render(package: TaskPackage, checker: PermissionChecker) -> str:
    """Render the package as the specialist's task context."""
    in_scope_lines = [f"- {s}" for s in package.in_scope] or ["- (as described above)"]
    lines = [
        f"TASK {package.task_id} — {package.task_objective}",
        f"Role: {package.target_role}",
        "",
        "IN SCOPE:",
        *in_scope_lines,
        "OUT OF SCOPE (do not touch):",
        *[f"- {s}" for s in package.out_of_scope],
        "",
    ]
    if package.instructions:
        lines += ["INSTRUCTIONS:", package.instructions, ""]
    if package.uncertainties:
        lines += ["UNCERTAINTIES:", *[f"- {u}" for u in package.uncertainties], ""]
    if package.conflicts:
        lines += ["CONFLICTS:", *[f"- {c}" for c in package.conflicts], ""]
    lines += ["ACCEPTANCE CRITERIA:", *[f"- {a}" for a in package.acceptance_criteria], ""]
    lines += ["VERIFICATION COMMANDS:", *[f"- {v}" for v in package.verification_commands], ""]
    lines += ["FAILURE CONDITIONS:", *[f"- {f}" for f in package.failure_conditions], ""]
    lines += ["ESCALATE WHEN:", *[f"- {e}" for e in package.escalation_conditions], ""]
    lines.append("RELEVANT CONTEXT (redacted excerpts):")
    for cf in package.context_files:
        resolved = checker.check_fs(package.target_role, "read", cf.path)
        lines += [f"--- {cf.path} ({cf.reason}) ---", excerpt_for(resolved)]
    return "\n".join(lines)
