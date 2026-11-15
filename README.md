# VISHWAKARMA

A persistent, auditable local AI engineering organisation. Specialist agents
(build, test, review, secure, operate software) execute real tasks against
real repositories under code-enforced permissions, deterministic quality
gates, and a persistent task ledger. The first product it builds is
**ReturnProof** (`../returnproof`).

## Status

| Milestone | Scope | State |
|---|---|---|
| F-000 | Scaffold, config, role grants, permission engine + security tests | **implemented, gates green** |
| F-001 | Persistent task ledger + state machine | **implemented, gates green** |
| F-002 | Model adapters (Ollama, FakeModel) + agent runtime | **implemented, gates green, live round-trip verified** |
| F-003 | Review gates + CLI | **implemented, gates green, self-hosted lifecycle verified** |
| F-004 | Prompt Architect runtime | **implemented, gates green, E2E PA mission verified** |
| F-005 | Chief Engineer runtime | **implemented, gates green, autonomous mission verified** |
| F-006 | Recovery + resumable execution | **implemented, gates green, live drill verified** |
| F-007 | Mission acceptance suite + opt-in live smoke | **implemented, gates green; deterministic FakeModel missions verified; live Ollama smoke is manual-only (opt-in) after Founder halted it — laptop RAM** |
| P-001 | Forge Trace: handoff logging & observability | **implemented, gates green; append-only handoff chain in ledger, secret-redacted, `forge trace show/failed` CLI, full-chain reconstruction tests** |

## Layout

```
forge/            runtime package (config, permissions, later: ledger, runtime, CLI)
forge/roles.yaml  single source of truth for agent permissions
tests/            permission attack-tests and component suites
docs/adr/         architecture decision records
```

## Run the gates

```
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest
.venv/Scripts/python -m ruff check .
```

## Governance invariants

1. Grants live in `forge/roles.yaml`; agents cannot grant themselves anything.
2. Deny overrides allow; a code-level deny list blocks destructive commands
   for every role.
3. No agent approves its own work; only gate-runner processes record gate
   results (once gates land in F-003).
