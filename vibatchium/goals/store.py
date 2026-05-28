"""SQLite persistence for Goals.

One file at ``CONFIG_DIR/goals.db`` (0600). Three tables: ``goals`` (one row
per goal, JSON payloads in columns for cheap status queries + atomic writes),
``goal_events`` (append-only, ``(goal_id, seq)`` PK), ``goal_artifacts``.

The store is pure persistence — no state-machine logic (that's the engine). All
methods are synchronous; the daemon runs in a single asyncio thread and these
queries are tiny. A module-level connection is reused with a lock so the engine
can call freely from async handlers.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from ..daemon.paths import CONFIG_DIR

GOALS_DB = CONFIG_DIR / "goals.db"
SCHEMA_VERSION = 1

# ─── ULID (lexicographically sortable, time-ordered ids) ─────────────────

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid() -> str:
    """A 26-char Crockford-base32 ULID: 48-bit ms timestamp + 80-bit random.
    Sortable by creation time, collision-resistant."""
    ts = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = secrets.randbits(80)
    value = (ts << 80) | rand
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


_DDL = """
CREATE TABLE IF NOT EXISTS goals (
  id TEXT PRIMARY KEY,
  description TEXT NOT NULL,
  session TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  status TEXT NOT NULL,
  budget_json TEXT NOT NULL,
  consumed_json TEXT NOT NULL DEFAULT '{}',
  inputs_json TEXT NOT NULL DEFAULT '{}',
  outputs_json TEXT,
  notifier TEXT,
  driver TEXT NOT NULL DEFAULT 'external',
  parent_id TEXT,
  caps TEXT,
  domain_allowlist TEXT,
  current_step INTEGER NOT NULL DEFAULT 0,
  checkpoint_id TEXT,
  client_token_idx TEXT NOT NULL DEFAULT '{}',
  pending_question TEXT
);
CREATE TABLE IF NOT EXISTS goal_events (
  goal_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (goal_id, seq)
);
CREATE TABLE IF NOT EXISTS goal_artifacts (
  goal_id TEXT NOT NULL,
  name TEXT NOT NULL,
  path TEXT NOT NULL,
  mime TEXT NOT NULL,
  size INTEGER NOT NULL,
  PRIMARY KEY (goal_id, name)
);
CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status);
"""


def _loads(val, default):
    if val is None:
        return default
    try:
        return json.loads(val)
    except (TypeError, ValueError):
        return default


class GoalStore:
    def __init__(self, db_path: Path | str | None = None):
        self.path = Path(db_path) if db_path is not None else GOALS_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        if new_file:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(_DDL)
            (cur,) = self._conn.execute("PRAGMA user_version").fetchone()
            if cur < SCHEMA_VERSION:
                self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ─── goals ───────────────────────────────────────────────────────────

    def create_goal(self, *, description: str, session: str, budget: dict,
                    inputs: dict | None = None, notifier: str | None = None,
                    driver: str = "external", parent_id: str | None = None,
                    caps: str | None = None,
                    domain_allowlist: str | None = None) -> dict:
        gid = ulid()
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO goals (id, description, session, created_at, "
                "updated_at, status, budget_json, consumed_json, inputs_json, "
                "notifier, driver, parent_id, caps, domain_allowlist) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (gid, description, session, now, now, "pending",
                 json.dumps(budget), "{}", json.dumps(inputs or {}),
                 notifier, driver, parent_id, caps, domain_allowlist),
            )
            self._conn.commit()
        return self.get_goal(gid)

    def get_goal(self, gid: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM goals WHERE id=?", (gid,)).fetchone()
        return self._row_to_goal(row) if row else None

    def list_goals(self, status: str | None = None) -> list[dict]:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM goals WHERE status=? ORDER BY created_at",
                    (status,)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM goals ORDER BY created_at").fetchall()
        return [self._row_to_goal(r) for r in rows]

    def list_children(self, parent_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM goals WHERE parent_id=? ORDER BY created_at",
                (parent_id,)).fetchall()
        return [self._row_to_goal(r) for r in rows]

    def update_goal(self, gid: str, **fields) -> None:
        """Update arbitrary goal columns. Dict-valued fields whose column name
        ends in ``_json`` (or are known JSON columns) are auto-serialized."""
        if not fields:
            return
        json_cols = {"budget": "budget_json", "consumed": "consumed_json",
                     "inputs": "inputs_json", "outputs": "outputs_json",
                     "client_token_idx": "client_token_idx"}
        sets, vals = [], []
        for k, v in fields.items():
            col = json_cols.get(k, k)
            if col.endswith("_json") or col == "client_token_idx":
                v = json.dumps(v)
            sets.append(f"{col}=?")
            vals.append(v)
        sets.append("updated_at=?")
        vals.append(int(time.time()))
        vals.append(gid)
        with self._lock:
            self._conn.execute(
                f"UPDATE goals SET {', '.join(sets)} WHERE id=?", vals)
            self._conn.commit()

    def _row_to_goal(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "description": row["description"],
            "session": row["session"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "status": row["status"],
            "budget": _loads(row["budget_json"], {}),
            "consumed": _loads(row["consumed_json"], {}),
            "inputs": _loads(row["inputs_json"], {}),
            "outputs": _loads(row["outputs_json"], None),
            "notifier": row["notifier"],
            "driver": row["driver"],
            "parent_id": row["parent_id"],
            "caps": row["caps"],
            "domain_allowlist": row["domain_allowlist"],
            "current_step": row["current_step"],
            "checkpoint_id": row["checkpoint_id"],
            "client_token_idx": _loads(row["client_token_idx"], {}),
            "pending_question": row["pending_question"],
        }

    # ─── events ────────────────────────────────────────────────────────────

    def append_event(self, gid: str, kind: str, payload: dict | None = None,
                     *, ts: float | None = None) -> int:
        with self._lock:
            (nxt,) = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM goal_events WHERE goal_id=?",
                (gid,)).fetchone()
            self._conn.execute(
                "INSERT INTO goal_events (goal_id, seq, ts, kind, payload_json) "
                "VALUES (?,?,?,?,?)",
                (gid, nxt, ts if ts is not None else time.time(), kind,
                 json.dumps(payload or {})),
            )
            self._conn.commit()
        return nxt

    def list_events(self, gid: str, after_seq: int = 0) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, ts, kind, payload_json FROM goal_events "
                "WHERE goal_id=? AND seq>? ORDER BY seq", (gid, after_seq)
            ).fetchall()
        return [{"seq": r["seq"], "ts": r["ts"], "kind": r["kind"],
                 "payload": _loads(r["payload_json"], {})} for r in rows]

    # ─── artifacts ─────────────────────────────────────────────────────────

    def add_artifact(self, gid: str, name: str, path: str, mime: str,
                     size: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO goal_artifacts "
                "(goal_id, name, path, mime, size) VALUES (?,?,?,?,?)",
                (gid, name, path, mime, size))
            self._conn.commit()

    def list_artifacts(self, gid: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, path, mime, size FROM goal_artifacts "
                "WHERE goal_id=?", (gid,)).fetchall()
        return [dict(r) for r in rows]
