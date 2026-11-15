# ADR-0006: Forge Trace — handoff observability in the task ledger

Status: Accepted · Date: 2026-11-15 · Milestone: P-001

## Context

Multi-agent failures concentrate at agent-to-agent boundaries (dispatch,
report, review, retry, escalation). The ledger already records state
transitions and gate results, and agent transcripts exist as JSONL, but
nothing answers, per task: WHO handled it, WHAT context they received, WHAT
they did, WHERE it failed, and whether the result WAS actually verified.

## Decision

Add one append-only SQLite table `handoffs` to the existing ledger database —
no new storage system, no new services. The Chief Engineer records a handoff
at every boundary it already crosses; no component is rewritten.

- Record: sender, receiver, kind (dispatch/report/verdict/gates/failure/
  retry/block), objective, prompt id + version, context refs, model,
  status, verified flag, redacted JSON detail (incl. tool-usage summary and
  transcript-derived data), timestamp.
- Parent linkage: each handoff stores `parent_id`, forming one connected
  timeline per task, including across retries and recoveries.
- Verification honesty: a report handoff is NEVER marked verified for merely
  returning text; `verified=True` only on gates green plus independent review
  CLEAN (the same rule the CE approval decision uses).
- Secrets: `redact_text` from the existing prompting/secrets module is applied
  to objective and detail before insert.
- Inspection: `forge trace show TASK` (timeline + state transitions) and
  `forge trace failed` (all failed/blocked handoffs, newest first).

## Consequences

- Lightweight: one table, one write per boundary, trigger-enforced
  append-only like `transitions`/`gate_results`.
- Transcripts keep full raw payloads; handoff records carry summaries — no
  duplication of transcript content.
- Bound by the same ledger file lifecycle; no cross-process lock issues.
- The P-001 test run exposed a latent test-fixture boundary bug (ReAct-shaped
  PA script silently yielding empty context under the single-shot PA) —
  evidence the trace does its job.
