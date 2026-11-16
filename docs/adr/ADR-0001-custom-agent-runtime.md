# ADR-0001: Lightweight custom agent runtime (no LangGraph/CrewAI)

**Date:** 2026-11-15 · **Status:** Accepted · **Decider:** Chief Engineer, Founder-approved

## Context

VISHWAKARMA needs a real multi-agent runtime on a 12 GB / 2-core Windows
machine with a single local 7B model, strict per-agent tool permissions, a
persistent task ledger, and deterministic quality gates.

## Options considered

- **CrewAI** — role-play oriented; weak per-agent filesystem permission
  enforcement; heavy dependency tree.
- **AutoGen/AG2** — conversation-centric; hard to bolt code-enforced tool
  permissions and our gate lifecycle onto.
- **LangGraph** — capable graph engine, but we would still hand-write
  permission checks, the ledger and gates; it buys a state machine we can
  write directly.
- **Lightweight custom runtime** — full control over the four things that
  matter here: tool-permission enforcement, bounded ReAct loops tolerant of
  weak model JSON, deterministic gates, audit ledger.

## Decision

Build a lightweight custom Python runtime (`forge/`): `pydantic`, `pyyaml`,
`httpx` only. Agent definitions are YAML data; permissions are enforced in
code (`forge/permissions.py`) before every tool call; one Ollama model serves
all agents sequentially; a `FakeModel` backend makes tests deterministic.

## Consequences

- We own ~1,000 lines of genuinely maintained code instead of adapting to a
  framework's conventions.
- Future parallel execution is supported by design (ledger task claiming is
  transactional), but not exercised on this hardware.
- We are responsible for protocol robustness against a 7B model's imperfect
  JSON — handled with bounded retries and schema validation, not trust.
