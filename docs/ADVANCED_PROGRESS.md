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

## A-002 — Portability of gates and configuration

- `run_gate` catches `FileNotFoundError`/`OSError` and records a FAILED gate
  (exit 127) instead of crashing the trusted runner.
- Gate commands support `{python}`, replaced with `sys.executable`, and bare
  `python`/`python3` are normalised to it. No `.venv/Scripts` paths remain.
- `forge.yaml` uses repo-relative workspaces and portable gate commands;
  `load_config` resolves workspace paths relative to the config file.
- `scripts/demo_greet.py` uses `sys.executable` and a temp-dir demo workspace
  (overridable with `FORGE_DEMO_DIR`); no hardcoded user paths.
- Added tests for missing interpreters, missing workspaces, the `{python}`
  placeholder and relative workspace resolution.
