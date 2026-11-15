# ADR-0003: Chief Engineer loop is deterministic mechanism invoking LLM agents

**Date:** 2026-11-16 · **Status:** Accepted

## Context

The Chief Engineer must be autonomous but auditable, running on a small local
model whose planning-level reasoning is the least reliable component.

## Decision

The CE goal loop (`forge/chief.py`) is deterministic Python: roadmap-driven
task selection, dependency-aware promotion, PA package preparation, specialist
dispatch, trusted gates, independent reviewer dispatch, approval rules
(gates green + review CLEAN), bounded revisions, escalation at budget
exhaustion, decision journal and daily reports.

LLM agents are invoked only where judgement is needed: PA curation,
specialist implementation, reviewer findings. Every agent run is bounded,
transcripted, and permission-checked. The CE itself never writes product
source code.

## Consequences

- Autonomy is auditable: every decision is a journal row; every agent turn is
  a transcript line; every state change is a ledger transition.
- The CE does not "reason" about priorities beyond explicit roadmap order;
  genuine planning judgement remains a human (founder) or future upgrade.
- Reviewer independence is structural (different role than implementer).
