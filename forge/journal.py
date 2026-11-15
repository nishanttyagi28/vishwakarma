"""Decision journal: every routine engineering decision, recorded with rationale.

Decisions are append-only (SQL-trigger enforced). Architecture-level decisions
are additionally written as ADR markdown files by the architect role.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    reversible INTEGER NOT NULL DEFAULT 1
);
CREATE TRIGGER IF NOT EXISTS decisions_no_update
BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete
BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;
"""


class DecisionJournal:
    def __init__(self, db_path: Path) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def record(self, kind: str, summary: str, rationale: str = "",
               task_id: str = "", reversible: bool = True) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO decisions (at, kind, task_id, summary, rationale, reversible)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (_now(), kind, task_id, summary, rationale, int(reversible)),
            )
        seq = cur.lastrowid
        return int(seq) if seq is not None else 0

    def recent(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM decisions ORDER BY seq DESC LIMIT ?", (limit,)
        ).fetchall()
