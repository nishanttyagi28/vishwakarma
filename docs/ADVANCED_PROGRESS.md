# Advanced hardening progress (A-001 .. A-010)

Short, factual log of the production-hardening milestones. Every milestone is
committed only after the full pytest suite, `ruff check .` and `mypy forge`
are green.

## A-001 — Shell tool security

- `shell.run` now executes an argv list with `shell=False`, in the task
  workspace, never through a shell.
- `forge.permissions.parse_shell_argv` rejects `&&`, `||`, `|`, `;`, `&`,
  `$(...)`, backticks, redirects, newlines and NUL before allowlist matching.
- Built-in `fs.*` and `shell.run` now call `check_tool` as well as their
  filesystem/shell grant.
- Attack tests cover every chaining, substitution and redirect vector at both
  the permission and tool-execution layers.
