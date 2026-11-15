# Usage

Everything below runs from the repository root with the project virtualenv. The CLI entry point
is a module: `.venv/Scripts/python -m forge.cli ...` (written `forge ...` in the docs for short).

## Verify the project

```
.venv/Scripts/python -m pytest
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m mypy forge
```

The live Ollama smoke test is skipped unless `FORGE_LIVE_SMOKE=1` is set.

## Configuration

- `forge.yaml` - workspaces, model assignment, limits and the trusted gate commands.
- `forge/roles.yaml` - per-role permission grants.
- Environment overrides for the CLI: `FORGE_CONFIG` (path to a `forge.yaml`) and `FORGE_DB` (path
  to a ledger SQLite file). They make it easy to run against a demo or copy of a ledger.

## The demo (no model needed)

```
.venv/Scripts/python scripts/demo_greet.py
```

Forge builds `greet.py` and `test_greet.py` into `C:/Users/Admin/projects/forge-demo-greet`
(the `DEMO` path at the top of the script) with `FakeModel` adapters, using its own ledger inside
that folder, so your main ledger is untouched. It prints the DEMO-1 outcome, the Forge Trace handoff
timeline, the gate result hash, and then runs the built project and its tests. Re-running is
deterministic: it clears the build artifacts and rebuilds.

Inspect the demo ledger:

```
$env:FORGE_CONFIG = "..\forge-demo-greet\forge.yaml"
$env:FORGE_DB     = "..\forge-demo-greet\forge-demo.sqlite3"
.venv/Scripts/python -m forge.cli task list
.venv/Scripts/python -m forge.cli task history DEMO-1
.venv/Scripts/python -m forge.cli trace show DEMO-1
```

## Forge CLI

Top-level commands: `task`, `gate`, `status`, `trace`, `recover`, `ui`, `chief`. Run any of them
with `--help` for details.

| Command | Purpose |
|---|---|
| `task list [--status S]` / `task show ID` / `task history ID` | Read tasks and their state transitions |
| `task create --id --title --role [--depends ...] [--human-gate] [--scope --exclusions --acceptance]` | Add a task to the ledger |
| `task promote / claim / submit / changes / revise / escalate / cancel / unblock / approve` | Lifecycle transitions, all through the enforced state machine (`approve` only by `chief` or `founder`) |
| `gate run TASK [--workspace W] [--timeout S]` | Run the trusted gates declared in `forge.yaml` and record hash-stamped results |
| `trace show TASK` / `trace failed` | Forge Trace: handoff timeline for a task / all failed or blocked handoffs, newest first |
| `recover [--revert]` | Detect interrupted runs and return them to READY; `--revert` is an explicit opt-in for reverting uncommitted files |
| `chief run` / `chief report` | Run the Chief Engineer loop / print the daily report |
| `ui [--port N] [--print]` | Read-only dashboard; `--print` writes the HTML to stdout instead of serving |

The lifecycle and `chief` commands above are listed from the CLI definitions in `forge/cli.py`;
`chief run` drives local-model agents and is meant for a machine with an Ollama model available.

## Forge UI

```
.venv/Scripts/python -m forge.cli ui --port 8765
```

Open `http://127.0.0.1:8765/` (default port 8765; Ctrl+C stops it). The page lists every task;
each task section shows the Forge Trace handoff timeline, gate results and state transitions. It
is read-only, GET-only, loopback-only, and needs no dependencies beyond the standard library.
Use `FORGE_DB` to point it at another ledger.