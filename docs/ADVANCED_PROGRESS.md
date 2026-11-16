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

## A-003 — Ledger-enforced approval invariants

- `TaskLedger.approve` now independently requires TESTING state, at least one
  recorded trusted gate result with every latest gate passing, and a verified
  CLEAN verdict from a reviewer whose role differs from the implementer.
- `task review` records an independent review verdict through the CLI, so real
  operators have a governance path to approval.
- `claim` writes the status change and its transition row in one transaction;
  a failed audit insert rolls the claim back.
- The backend role lost `forge/**` write access; governance files
  (`roles.yaml`, `permissions.py`, `ledger.py`, `gates.py`, `chief.py`,
  `cli.py`) are not writable by any agent. Backend keeps the runtime, model and
  prompting packages.
- Replaced the vacuous revision-budget assertion with a real history check and
  added approval-invariant and atomic-claim tests.

## A-004 — CLI and Chief Engineer refactor

- `forge/cli.py` now has a parser builder plus per-command handlers
  (`_handle_task`, `_handle_gate`, `_handle_recover`, `_handle_status`,
  `_handle_trace`, `_handle_ui`, `_handle_chief`) instead of one large
  function. `execute()` keeps its text return for callers;
  `execute_result()` exposes a typed `exit_code`.
- Gate failure now returns `EXIT_GATES_FAILED` (3) rather than matching the
  "GATES FAILED" string in `main`.
- `chief.py` splits `execute_task` into `_ensure_running`, `_prepare_task`,
  `_attempt`, `_review_verdict`, `_approve_attempt` and `_apply_revision`.
- Added the `forge = "forge.cli:main"` project script entry point.
- `chief run --model fake|ollama` selects a scripted `FakeModel` or the local
  Ollama backend; tests use `fake` only.
- The Chief command reuses the CLI's ledger instance (single open/close) and
  closes only its own registry and journal.
