"""Deterministic permission enforcement for VISHWAKARMA agent tool calls.

Every agent tool call passes through this module before execution. Grants live
in ``forge/roles.yaml`` — declared by governance, never by an agent. Deny rules
always override allow rules, and a code-level deny list blocks commands that no
role may ever run, whatever any file or prompt claims.

Path specs have the form ``workspace:relative/path`` (e.g.
``vishwakarma:forge/permissions.py``). A workspace-relative grant of ``""``
means the whole workspace. ``..`` segments that would escape the workspace root
are rejected.
"""

import shlex
from pathlib import Path, PurePosixPath

import yaml
from pydantic import BaseModel, Field


class PermissionDenied(Exception):
    """An agent attempted an action its role does not grant."""


# Commands no role may ever execute, whatever roles.yaml says.
GLOBAL_SHELL_DENY = (
    "rm",
    "del",
    "erase",
    "rmdir",
    "rd",
    "format",
    "diskpart",
    "shutdown",
    "taskkill",
    "reg",
    "regedit",
    "remove-item",
    "set-executionpolicy",
    "git reset",
    "git checkout --",
    "git restore",
    "git clean",
    "git push --force",
    "git push -f",
)


def normalize_rel(rel: str) -> str | None:
    """Normalise a workspace-relative path; None if it escapes the workspace root."""
    p = PurePosixPath(rel.replace("\\", "/"))
    parts: list[str] = []
    for seg in p.parts:
        if seg in ("", "."):
            continue
        if seg == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(seg)
    return "/".join(parts)


def split_spec(spec: str) -> tuple[str, str]:
    """Split ``workspace:relative/path`` into (workspace, relative path)."""
    ws, sep, rel = spec.partition(":")
    if not sep or not ws.strip():
        raise ValueError(f"path spec must be 'workspace:relative/path', got {spec!r}")
    return ws.strip(), rel


class FsGrants(BaseModel):
    read: list[str] = Field(default_factory=list)
    write: list[str] = Field(default_factory=list)


class RoleGrant(BaseModel):
    role: str
    mission: str = ""
    fs: FsGrants = Field(default_factory=FsGrants)
    shell_allow: list[str] = Field(default_factory=list)
    shell_deny: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)


def load_roles(path: Path) -> dict[str, RoleGrant]:
    """Load role grants from YAML. Unknown fields fail validation."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    grants: dict[str, RoleGrant] = {}
    for raw in data["roles"]:
        grant = RoleGrant.model_validate(raw)
        if grant.role in grants:
            raise ValueError(f"duplicate role {grant.role!r} in {path}")
        grants[grant.role] = grant
    return grants


def _covered(prefixes: list[str], ws: str, norm: str) -> bool:
    """True if any grant covers the workspace-relative path ``norm``.

    Grant syntax: ``""`` grants the whole workspace; ``dir/**`` grants the
    directory and everything under it; ``dir/file.ext`` grants exactly that.
    """
    for prefix in prefixes:
        pws, prel = split_spec(prefix)
        if pws != ws:
            continue
        pnorm = normalize_rel(prel)
        if pnorm is None:
            continue
        if pnorm == "":
            return True
        base = pnorm[:-3] if pnorm.endswith("/**") else pnorm
        if norm == base or norm.startswith(base + "/"):
            return True
    return False


#: Shell metacharacters that enable chaining, substitution or redirection.
#: Commands are executed as argv lists with ``shell=False``; any of these in a
#: request is refused outright rather than interpreted.
SHELL_METACHARACTERS = ("&", "|", ";", "$", "`", ">", "<", "\n", "\r", "\x00")


def parse_shell_argv(command: str) -> list[str]:
    """Parse a shell request into an argv list, refusing shell metacharacters.

    There is no shell between the request and the process, so quoting is only
    used to group words. Anything that a POSIX shell would treat specially is
    rejected before parsing, which closes allowlist-prefix bypasses such as
    ``pytest && touch X``, ``pytest | tee out``, ``$(...)`` and backticks.
    """
    if not command.strip():
        raise PermissionDenied("empty command")
    for char in SHELL_METACHARACTERS:
        if char in command:
            raise PermissionDenied(
                f"shell metacharacter {char!r} is not allowed in commands"
            )
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise PermissionDenied(f"malformed command: {exc}") from exc
    if not argv:
        raise PermissionDenied("empty command")
    return argv


def _words(command: str) -> list[str]:
    return [w.lower() for w in parse_shell_argv(command)]


def _matches_rule(words: list[str], rule: str) -> bool:
    rule_words = [w.lower() for w in shlex.split(rule, posix=True)]
    if not rule_words:
        return False
    return words[: len(rule_words)] == rule_words


class PermissionChecker:
    """Validates agent requests against role grants. Pure functions, no side effects."""

    def __init__(self, grants: dict[str, RoleGrant], workspaces: dict[str, str]) -> None:
        self.grants = grants
        self.workspaces = {name: Path(p).resolve() for name, p in workspaces.items()}

    def _grant(self, role: str) -> RoleGrant:
        try:
            return self.grants[role]
        except KeyError:
            raise PermissionDenied(f"unknown role {role!r}") from None

    def workspace_root(self, name: str) -> Path:
        try:
            return self.workspaces[name]
        except KeyError:
            raise PermissionDenied(f"unknown workspace {name!r}") from None

    def check_fs(self, role: str, action: str, spec: str) -> Path:
        """Validate an fs.read/fs.write request; return the resolved absolute path."""
        if action not in ("read", "write"):
            raise ValueError(f"unknown fs action {action!r}")
        grant = self._grant(role)
        ws, rel = split_spec(spec)
        norm = normalize_rel(rel)
        if norm is None:
            raise PermissionDenied(f"path escapes workspace root: {spec!r}")
        prefixes = grant.fs.read if action == "read" else grant.fs.write
        if not _covered(prefixes, ws, norm):
            raise PermissionDenied(f"role {role!r} has no {action} grant for {spec!r}")
        return self.workspace_root(ws) / norm

    def check_shell(self, role: str, command: str) -> None:
        """Validate a shell request against deny-then-allow rules. Raises if refused."""
        grant = self._grant(role)
        words = _words(command)
        for rule in (*GLOBAL_SHELL_DENY, *grant.shell_deny):
            if _matches_rule(words, rule):
                raise PermissionDenied(
                    f"command matches deny rule {rule!r} for role {role!r}"
                )
        if not any(_matches_rule(words, rule) for rule in grant.shell_allow):
            raise PermissionDenied(f"command {command!r} not in allowlist for {role!r}")

    def check_tool(self, role: str, tool: str) -> None:
        """Validate a named-tool request against the role's tool list."""
        grant = self._grant(role)
        if tool not in grant.tools:
            raise PermissionDenied(f"role {role!r} has no tool {tool!r}")
