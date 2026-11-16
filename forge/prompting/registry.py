"""Prompt registry: versioned prompts, outcome tracking, bounded revisions.

Prompts are never edited: a change supersedes the old version. Outcomes are
recorded from real task runs (ledger-backed), and the PA may revise a prompt
at most ``max_revisions`` times per failure pattern before it must escalate to
a human — no infinite optimisation loops.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

APPROVED_FIRST_PASS = "APPROVED_FIRST_PASS"
REVISED_ONCE = "REVISED_ONCE"
REVISED_TWICE = "REVISED_TWICE"
BLOCKED_HUMAN = "BLOCKED_HUMAN"
FAILED = "FAILED"

OUTCOME_CLASSES = {APPROVED_FIRST_PASS, REVISED_ONCE, REVISED_TWICE, BLOCKED_HUMAN, FAILED}


class RegistryError(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class PromptVersion(BaseModel):
    prompt_id: str
    version: int
    target_role: str
    task_category: str
    package_sha256: str
    superseded_by: int | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS prompts (
    prompt_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    target_role TEXT NOT NULL,
    task_category TEXT NOT NULL,
    package_sha256 TEXT NOT NULL,
    superseded_by INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY (prompt_id, version)
);
CREATE TABLE IF NOT EXISTS prompt_outcomes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    outcome_class TEXT NOT NULL,
    at TEXT NOT NULL
);
"""


class PromptRegistry:
    def __init__(self, db_path: Path, max_revisions: int = 2) -> None:
        self.db_path = Path(db_path)
        self._conn = sqlite3.connect(self.db_path, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self.max_revisions = max_revisions

    def close(self) -> None:
        self._conn.close()

    def register(
        self,
        prompt_id: str,
        target_role: str,
        task_category: str,
        package_sha256: str,
    ) -> int:
        """Register the next version of a prompt. Refused beyond the revision budget."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM prompts WHERE prompt_id = ?", (prompt_id,)
        ).fetchone()
        version = row["n"] + 1
        if version > self.max_revisions + 1:
            raise RegistryError(
                f"prompt {prompt_id!r} exceeded {self.max_revisions} revisions; "
                "escalate to a human instead of revising again"
            )
        with self._conn:
            self._conn.execute(
                "INSERT INTO prompts (prompt_id, version, target_role, task_category,"
                " package_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (prompt_id, version, target_role, task_category, package_sha256, _now()),
            )
        return version

    def supersede(self, prompt_id: str, old_version: int, new_version: int) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE prompts SET superseded_by = ? WHERE prompt_id = ? AND version = ?",
                (new_version, prompt_id, old_version),
            )

    def active_version(self, prompt_id: str) -> PromptVersion | None:
        row = self._conn.execute(
            "SELECT * FROM prompts WHERE prompt_id = ? AND superseded_by IS NULL"
            " ORDER BY version DESC LIMIT 1",
            (prompt_id,),
        ).fetchone()
        if row is None:
            return None
        return PromptVersion(
            prompt_id=row["prompt_id"],
            version=row["version"],
            target_role=row["target_role"],
            task_category=row["task_category"],
            package_sha256=row["package_sha256"],
            superseded_by=row["superseded_by"],
        )

    def record_outcome(self, prompt_id: str, version: int, run_id: str, outcome_class: str) -> None:
        if outcome_class not in OUTCOME_CLASSES:
            raise RegistryError(f"unknown outcome class {outcome_class!r}")
        with self._conn:
            self._conn.execute(
                "INSERT INTO prompt_outcomes (prompt_id, version, run_id, outcome_class, at)"
                " VALUES (?, ?, ?, ?, ?)",
                (prompt_id, version, run_id, outcome_class, _now()),
            )

    def first_pass_rate(self, prompt_id: str) -> float | None:
        rows = self._conn.execute(
            "SELECT outcome_class FROM prompt_outcomes WHERE prompt_id = ?",
            (prompt_id,),
        ).fetchall()
        if not rows:
            return None
        first_pass = sum(1 for r in rows if r["outcome_class"] == APPROVED_FIRST_PASS)
        return first_pass / len(rows)

    def outcome_count(self, prompt_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM prompt_outcomes WHERE prompt_id = ?", (prompt_id,)
        ).fetchone()
        return row["n"]
