# Usage

Run everything from the repository root with the project virtualenv. Use
`.venv/bin/python` on Linux/macOS or `.venv/Scripts/python` on Windows; after
`pip install -e .` the `forge` entry point is equivalent to `python -m forge.cli`.

## Verify the project

```bash
python -m pytest --cov=forge --cov-report=term-missing
python -m ruff check .
python -m mypy forge
```

Coverage has an enforced 88% floor (currently ~90%). The live Ollama smoke test
is skipped unless `FORGE_LIVE_SMOKE=1` is set; every other test uses `FakeModel`
or mocked HTTP and starts no model.

## Configuration

- `forge.yaml` — repo-relative workspaces, model assignment, limits, trusted gate commands.
  `{python}` in a gate command resolves to the interpreter running Forge.
- `forge/roles.yaml` — per-role permission grants and implemented tools.
- `plugins/` — optional custom gate runners and additive roles ([PLUGINS.md](PLUGINS.md)).
- Environment overrides:
  - `FORGE_CONFIG` — path to a `forge.yaml`.
  - `FORGE_DB` — path to a ledger SQLite file.
  - `FORGE_PLUGINS_DIR` — alternate plugins folder.
  - `FORGE_OPENAI_BASE_URL`, `FORGE_OPENAI_MODEL`, `FORGE_OPENAI_API_KEY` — OpenAI-compatible
    adapter; optional `FORGE_INPUT_COST_PER_MTOK` and `FORGE_OUTPUT_COST_PER_MTOK` rates.
  - `FORGE_DEMO_DIR` — where `scripts/demo_greet.py` builds its demo project.
  - `FORGE_LIVE_SMOKE=1` — opt in to the live Ollama adapter smoke test.

Optional budgets in `forge.yaml` (`limits`): `max_tokens_per_task`,
`max_tokens_per_mission`, `max_cost_usd_per_task`, `max_cost_usd_per_mission`.
A breach escalates the task to BLOCKED_HUMAN after recording the usage that
caused it. `max_parallel_workers` controls how many independent roadmap tasks
may run concurrently when a per-task model factory is supplied.

## The demo (no model needed)

```bash
python scripts/demo_greet.py
```

Forge builds `greet.py` and `test_greet.py` into a temporary folder (or
`FORGE_DEMO_DIR`) with `FakeModel` adapters, using its own ledger inside that
folder, so your main ledger is untouched. It prints the DEMO-1 outcome, the
Forge Trace handoff timeline, the gate result hash, then runs the built project
and its tests. Re-running is deterministic: it clears build artifacts and
rebuilds.

Inspect a demo ledger by pointing the CLI at the demo files:

```bash
export FORGE_CONFIG="$FORGE_DEMO_DIR/forge.yaml"
export FORGE_DB="$FORGE_DEMO_DIR/forge-demo.sqlite3"
python -m forge.cli task list
python -m forge.cli task history DEMO-1
python -m forge.cli trace show DEMO-1
```

On PowerShell use `$env:FORGE_CONFIG = "..."` / `$env:FORGE_DB = "..."`.

## Forge CLI

Top-level commands: `task`, `gate`, `status`, `trace`, `recover`, `ui`, `chief`.
Run any of them with `--help` for details.

| Command | Purpose |
|---|---|
| `task list [--status S]` / `task show ID` / `task history ID` | Read tasks and their state transitions |
| `task create --id --title --role [--depends ...] [--human-gate] [--scope --exclusions --acceptance]` | Add a task to the ledger |
| `task promote / claim / submit / changes / revise / escalate / cancel / unblock` | Lifecycle transitions through the enforced state machine |
| `task review TASK --reviewer ROLE --verdict CLEAN\|ISSUES [--findings ...]` | Record an independent review verdict (reviewer must differ from the implementer) |
| `task approve TASK --actor chief\|founder [--note ...]` | Approve only when TESTING, latest gates are green and the latest independent verdict is verified CLEAN |
| `gate run TASK [--workspace W] [--timeout S]` | Run trusted gates declared in `forge.yaml` (or plugin runners) and record hash-stamped results; exits 3 when gates fail |
| `trace show TASK` / `trace failed` | Forge Trace: handoff timeline, usage rows, state transitions / all failed or blocked handoffs, newest first |
| `recover [--revert]` | Detect interrupted runs and return them to READY; `--revert` is an explicit opt-in for reverting uncommitted files |
| `chief run --task ID [--model fake\|ollama\|openai]` / `chief report` | Run the Chief Engineer loop / print the daily report |
| `ui [--port N] [--print]` | Read-only dashboard; `--print` writes the HTML to stdout instead of serving |

`chief run --model fake` uses scripted, no-network backends. `ollama` is the
default local backend; `openai` uses the `FORGE_OPENAI_*` configuration.

## Forge UI

```bash
python -m forge.cli ui --port 8765
```

Open `http://127.0.0.1:8765/`. Views:

- `/` — all tasks, mission token/cost usage, and per-task trace sections.
- `/?task=TASK` — task detail: metadata, usage, handoff graph, handoff timeline, gate results,
  gate-result diffs and state transitions.
- `/?mission=MISSION` — mission timeline with chronological usage and links to its tasks.

The page auto-refreshes every 5 seconds. It is read-only, GET-only,
loopback-only (`127.0.0.1`), uses only the standard library, and loads no
external assets. Use `FORGE_DB` to point it at another ledger.

## Plugins

Custom gate runners and additive roles can be loaded from `plugins/*.py` or the
`vishwakarma.plugins` entry-point group. See [PLUGINS.md](PLUGINS.md) and the
working example in [`plugins/example_gate.py`](../plugins/example_gate.py).
Plugin code is trusted operator code; it cannot override built-in roles, and
broken runners are recorded as FAILED gates.
