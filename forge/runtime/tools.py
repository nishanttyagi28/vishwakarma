"""Permission-checked tool execution for agents.

Every tool call goes through ``PermissionChecker`` before touching the
filesystem or a shell. Denials are returned as observations (the agent may
correct course) and are counted by the runtime; repeated denial aborts the
run. Tools are the ONLY path by which an agent changes a repository.
"""

import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from forge.permissions import PermissionChecker, PermissionDenied

MAX_OUTPUT_CHARS = 4000


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
    ) -> None:
        self.checker = checker
        self.role = role
        self.shell_timeout_s = shell_timeout_s
        self.named_tools = named_tools or {}
        self.default_workspace = default_workspace

    @property
    def tool_names(self) -> list[str]:
        base = ["fs.read", "fs.write", "fs.list", "shell.run"]
        return base + sorted(self.named_tools)

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
            if tool == "fs.read":
                return self._fs_read(args)
            if tool == "fs.write":
                return self._fs_write(args)
            if tool == "fs.list":
                return self._fs_list(args)
            if tool == "shell.run":
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

    def _shell_run(self, args: dict[str, object]) -> ToolResult:
        command = str(args.get("command", ""))
        self.checker.check_shell(self.role, command)
        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.shell_timeout_s,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(False, f"command timed out after {self.shell_timeout_s}s")
        combined = (proc.stdout or "") + (proc.stderr or "")
        return ToolResult(True, _truncate(f"exit={proc.returncode}\n{combined}"))
