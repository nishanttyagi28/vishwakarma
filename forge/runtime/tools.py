"""Permission-checked tool execution for agents.

Every tool call goes through ``PermissionChecker`` before touching the
filesystem or a shell. Denials are returned as observations (the agent may
correct course) and are counted by the runtime; repeated denial aborts the
run. Tools are the ONLY path by which an agent changes a repository.
"""

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from forge.permissions import PermissionChecker, PermissionDenied, parse_shell_argv

if TYPE_CHECKING:
    from forge.ledger import TaskLedger

MAX_OUTPUT_CHARS = 4000
BUILTIN_NAMED_TOOLS = ("repo.search", "prompt.save", "ledger.query")
SKIP_DIRS = {".git", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache",
             ".ruff_cache", "node_modules", "dist", "build"}


@dataclass
class ToolResult:
    ok: bool
    output: str
    denied: bool = False


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated, {len(text)} chars total]"


class ToolExecutor:
    """Binds the permission checker to a role and executes named tools.

    ``named_tools`` registers role-specific tools (e.g. the PA's repo.search).
    They are dispatched only after ``check_tool`` confirms the role's grant.
    """

    def __init__(
        self,
        checker: PermissionChecker,
        role: str,
        shell_timeout_s: float = 120.0,
        named_tools: dict[str, Callable[[dict[str, object]], ToolResult]] | None = None,
        default_workspace: str | None = None,
        ledger: "TaskLedger | None" = None,
    ) -> None:
        self.checker = checker
        self.role = role
        self.shell_timeout_s = shell_timeout_s
        self.named_tools = named_tools or {}
        self.default_workspace = default_workspace
        self.ledger = ledger

    @property
    def tool_names(self) -> list[str]:
        base = ["fs.read", "fs.write", "fs.list", "shell.run"]
        grant = self.checker.grants.get(self.role)
        granted = set(grant.tools) if grant else set()
        builtin = [name for name in BUILTIN_NAMED_TOOLS if name in granted]
        extra = sorted(set(self.named_tools) - set(builtin))
        return base + builtin + extra

    def _resolve(self, spec: str) -> str:
        """Normalise a path spec, defaulting prefix-less paths to the task's
        workspace. Errors teach the exact expected format with an example."""
        spec = spec.strip()
        if ":" in spec:
            return spec
        if self.default_workspace:
            return f"{self.default_workspace}:{spec}"
        raise ValueError(
            "path spec must be 'workspace:relative/path' - for example "
            f"'{self.default_workspace or 'proj'}:src/main.py' - got {spec!r}"
        )

    def execute(self, tool: str, args: dict[str, object]) -> ToolResult:
        try:
            if tool in self.named_tools:
                self.checker.check_tool(self.role, tool)
                return self.named_tools[tool](args)
            if tool in BUILTIN_NAMED_TOOLS:
                self.checker.check_tool(self.role, tool)
                return getattr(self, "_" + tool.replace(".", "_"))(args)
            if tool in ("fs.read", "fs.write", "fs.list"):
                self.checker.check_tool(self.role, "fs")
                return getattr(self, "_" + tool.replace(".", "_"))(args)
            if tool == "shell.run":
                self.checker.check_tool(self.role, "shell")
                return self._shell_run(args)
            return ToolResult(
                False,
                f"unknown tool {tool!r}; available tools: {', '.join(self.tool_names)}",
            )
        except PermissionDenied as exc:
            return ToolResult(False, f"PERMISSION_DENIED: {exc}", denied=True)
        except (ValueError, OSError) as exc:
            # invalid arguments are observations for the agent, not crashes
            return ToolResult(False, f"TOOL_ERROR: {exc}")
    # --- tools ---------------------------------------------------------------

    def _fs_read(self, args: dict[str, object]) -> ToolResult:
        spec = self._resolve(str(args.get("path", "")))
        path = self.checker.check_fs(self.role, "read", spec)
        if not path.is_file():
            return ToolResult(False, f"not a file: {spec}")
        text = path.read_text(encoding="utf-8", errors="replace")
        return ToolResult(True, _truncate(text))

    def _fs_write(self, args: dict[str, object]) -> ToolResult:
        spec = self._resolve(str(args.get("path", "")))
        content = str(args.get("content", ""))
        path = self.checker.check_fs(self.role, "write", spec)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        return ToolResult(True, f"wrote {len(content)} chars to {spec}")

    def _fs_list(self, args: dict[str, object]) -> ToolResult:
        spec = self._resolve(str(args.get("path", "")))
        path = self.checker.check_fs(self.role, "read", spec)
        if not path.is_dir():
            return ToolResult(False, f"not a directory: {spec}")
        entries = []
        for child in sorted(path.iterdir()):
            entries.append(child.name + ("/" if child.is_dir() else ""))
        return ToolResult(True, _truncate("\n".join(entries) or "(empty)"))

    def _repo_search(self, args: dict[str, object]) -> ToolResult:
        query = str(args.get("query", "")).strip()
        if not query:
            raise ValueError("repo.search requires a non-empty 'query'")
        path_arg = str(args.get("path", "")).strip()
        if path_arg:
            spec = self._resolve(path_arg)
        elif self.default_workspace:
            spec = f"{self.default_workspace}:"
        else:
            raise ValueError("repo.search requires a path or a default workspace")
        root = self.checker.check_fs(self.role, "read", spec)
        if not root.exists():
            return ToolResult(False, f"path does not exist: {spec}")
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        hits: list[str] = []
        scanned = 0
        for candidate in candidates:
            if len(hits) >= 50 or scanned >= 500:
                break
            if not candidate.is_file():
                continue
            if any(part in SKIP_DIRS for part in candidate.parts):
                continue
            if candidate.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".pdf",
                                            ".zip", ".pyc", ".sqlite3"}:
                continue
            scanned += 1
            try:
                lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            base = root if root.is_dir() else root.parent
            for number, line in enumerate(lines, start=1):
                if query.lower() in line.lower():
                    rel = candidate.relative_to(base).as_posix()
                    hits.append(f"{rel}:{number}: {line.strip()[:200]}")
                    if len(hits) >= 50:
                        break
        return ToolResult(True, _truncate("\n".join(hits) or "(no matches)"))

    def _prompt_save(self, args: dict[str, object]) -> ToolResult:
        name = str(args.get("name", "")).strip()
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
        if not safe:
            raise ValueError("prompt.save requires a non-empty 'name'")
        if not self.default_workspace:
            raise ValueError("prompt.save requires a default workspace")
        spec = f"{self.default_workspace}:forge/prompts/drafts/{safe}.md"
        return self._fs_write({"path": spec, "content": str(args.get("content", ""))})

    def _ledger_query(self, args: dict[str, object]) -> ToolResult:
        if self.ledger is None:
            return ToolResult(False, "ledger.query unavailable in this runtime")
        task_id = str(args.get("task_id", "")).strip()
        try:
            if task_id:
                task = self.ledger.get(task_id)
                return ToolResult(True, _truncate(
                    f"{task.id} [{task.status}] {task.assignee_role} - {task.title}"
                ))
            status = str(args.get("status", "")).strip() or None
            tasks = self.ledger.list_tasks(status)
        except Exception as exc:  # ledger errors are observations for the agent
            return ToolResult(False, f"ledger error: {exc}")
        lines = [f"{t.id} [{t.status}] {t.assignee_role} - {t.title}" for t in tasks]
        return ToolResult(True, _truncate("\n".join(lines) or "(no tasks)"))

    def _shell_run(self, args: dict[str, object]) -> ToolResult:
        command = str(args.get("command", ""))
        self.checker.check_shell(self.role, command)
        argv = parse_shell_argv(command)
        cwd = None
        if self.default_workspace:
            try:
                cwd = self.checker.workspace_root(self.default_workspace)
            except PermissionDenied:
                cwd = None
        try:
            proc = subprocess.run(
                argv,
                shell=False,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=self.shell_timeout_s,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(False, f"command timed out after {self.shell_timeout_s}s")
        except OSError as exc:
            return ToolResult(False, f"TOOL_ERROR: cannot execute {argv[0]!r}: {exc}")
        combined = (proc.stdout or "") + (proc.stderr or "")
        return ToolResult(True, _truncate(f"exit={proc.returncode}\n{combined}"))
