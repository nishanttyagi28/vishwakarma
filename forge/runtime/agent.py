"""Bounded agent runtime: one role, one session, one shared local model.

The loop is strict-JSON: each turn is either a tool call or a final
ImplementationReport. Malformed model output is corrected at most
``MAX_MALFORMED`` times before the run fails with FAILED_MODEL. Permission
denials are observed; more than ``MAX_DENIALS`` aborts the run with
FAILED_PERMISSION. The step budget bounds every run. Every turn is appended
to a JSONL transcript on disk.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from forge.model.base import Message, ModelBackend
from forge.runtime.tools import ToolExecutor

MAX_MALFORMED = 3
MAX_DENIALS = 3

COMPLETED = "COMPLETED"
FAILED_MODEL = "FAILED_MODEL"
FAILED_MODEL_ERROR = "FAILED_MODEL_ERROR"
FAILED_PERMISSION = "FAILED_PERMISSION"
FAILED_BUDGET = "FAILED_BUDGET"


class ImplementationReport(BaseModel):
    summary: str
    files_changed: list[str] = Field(default_factory=list)
    tests_run: list[str] = Field(default_factory=list)
    self_assessment: str = ""
    open_issues: list[str] = Field(default_factory=list)
    artifacts: dict[str, object] = Field(default_factory=dict)
    """Role-specific structured output (e.g. the PA's curated context list)."""


class RunResult(BaseModel):
    run_id: str
    role: str
    status: str
    report: ImplementationReport | None = None
    steps_used: int
    malformed_turns: int = 0
    denials: int = 0
    transcript_path: str
    failure_reason: str = ""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def build_system_prompt(role: str, mission: str) -> str:
    return f"""You are the {role} agent inside VISHWAKARMA.

Mission: {mission}

Reply protocol — exactly ONE JSON object per turn, no other text:
{{"thought": "<brief reasoning>", "action": {{"tool": "<tool>", "args": {{...}}}}}}
or, when the work is finished:
{{"thought": "<brief reasoning>", "final": {{"summary": "...",
  "files_changed": ["..."], "tests_run": ["..."], "self_assessment": "...",
  "open_issues": ["..."]}}}}

Rules:
- Only claim what you actually did this run.
- Never fabricate test results; list only commands you ran.
- If a tool returns PERMISSION_DENIED, stop attempting that action and adjust.
- Finalise as soon as the objective is met; do not pad with extra steps."""


class AgentRuntime:
    """Runs one agent session to completion under bounded limits."""

    def __init__(
        self,
        model: ModelBackend,
        tools: ToolExecutor,
        role: str,
        mission: str,
        max_steps: int,
        transcript_dir: Path,
    ) -> None:
        self.model = model
        self.tools = tools
        self.role = role
        self.system_prompt = build_system_prompt(role, mission)
        self.max_steps = max_steps
        self.transcript_dir = transcript_dir
        self.malformed_turns = 0
        self.denials = 0
        self.steps_used = 0

    # --- transcript ----------------------------------------------------------

    def _transcript(self, run_id: str) -> Path:
        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        return self.transcript_dir / f"{run_id}.jsonl"

    def _log(self, path: Path, kind: str, data: dict[str, object]) -> None:
        record = {"at": _now(), "type": kind, **data}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    # --- main loop -----------------------------------------------------------

    def run(self, run_id: str, context: str) -> RunResult:
        self.malformed_turns = 0
        self.denials = 0
        self.steps_used = 0
        path = self._transcript(run_id)
        self._log(path, "run_start", {"run_id": run_id, "role": self.role})

        messages: list[Message] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": context},
        ]

        while self.steps_used < self.max_steps:
            self.steps_used += 1
            try:
                raw = self.model.chat(messages, json_mode=True)
            except Exception as exc:  # backend failure is a run failure, not a crash
                self._log(path, "model_error", {"error": str(exc)})
                return self._result(run_id, path, FAILED_MODEL_ERROR, str(exc))

            self._log(path, "model_raw", {"content": raw})
            try:
                turn = json.loads(raw)
                if not isinstance(turn, dict):
                    raise ValueError("turn is not a JSON object")
            except ValueError as exc:
                self.malformed_turns += 1
                if self.malformed_turns > MAX_MALFORMED:
                    reason = f"model produced malformed JSON {self.malformed_turns} times"
                    self._log(path, "failure", {"reason": reason})
                    return self._result(run_id, path, FAILED_MODEL, reason)
                messages.append({"role": "assistant", "content": raw})
                messages.append(
                    {
                        "role": "user",
                        "content": f"Your reply was not valid JSON ({exc}). "
                        "Reply again with exactly ONE JSON object per the protocol.",
                    }
                )
                continue

            if "final" in turn:
                try:
                    report = ImplementationReport.model_validate(turn["final"])
                except Exception as exc:
                    reason = f"final report failed validation: {exc}"
                    self._log(path, "failure", {"reason": reason})
                    return self._result(run_id, path, FAILED_MODEL, reason)
                self._log(path, "final", {"report": report.model_dump()})
                return self._result(run_id, path, COMPLETED, report=report)

            action = turn.get("action")
            if not isinstance(action, dict) or "tool" not in action:
                self.malformed_turns += 1
                if self.malformed_turns > MAX_MALFORMED:
                    reason = "model kept replying without a valid action or final"
                    self._log(path, "failure", {"reason": reason})
                    return self._result(run_id, path, FAILED_MODEL, reason)
                messages.append({"role": "assistant", "content": json.dumps(turn)})
                messages.append(
                    {
                        "role": "user",
                        "content": "That turn had no valid 'action' or 'final' object. "
                        "Reply again with exactly ONE JSON object per the protocol.",
                    }
                )
                continue

            tool_name = str(action.get("tool", ""))
            args = action.get("args")
            args = args if isinstance(args, dict) else {}
            result = self.tools.execute(tool_name, args)
            if result.denied:
                self.denials += 1
                self._log(path, "permission_denied", {"tool": tool_name})
                if self.denials > MAX_DENIALS:
                    reason = f"{self.denials} permission denials; aborting run"
                    self._log(path, "failure", {"reason": reason})
                    return self._result(run_id, path, FAILED_PERMISSION, reason)
            else:
                self._log(path, "tool", {"tool": tool_name, "ok": result.ok})
            messages.append({"role": "assistant", "content": json.dumps(turn)})
            messages.append(
                {"role": "user", "content": f"OBSERVATION ({tool_name}): {result.output}"}
            )

        reason = f"step budget of {self.max_steps} exhausted"
        self._log(path, "failure", {"reason": reason})
        return self._result(run_id, path, FAILED_BUDGET, reason)

    def _result(
        self,
        run_id: str,
        path: Path,
        status: str,
        reason: str = "",
        report: ImplementationReport | None = None,
    ) -> RunResult:
        return RunResult(
            run_id=run_id,
            role=self.role,
            status=status,
            report=report,
            steps_used=self.steps_used,
            malformed_turns=self.malformed_turns,
            denials=self.denials,
            transcript_path=str(path),
            failure_reason=reason,
        )
