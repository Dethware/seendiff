"""SQLite review state and prefs. No ORM, no migrations (DEVELOPMENT.md,
"Review state")."""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone

PRUNE_DAYS = 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS block_state (
  scope       TEXT NOT NULL,
  path        TEXT NOT NULL,
  hunk_id     TEXT NOT NULL,
  seen_at     TEXT,
  reviewed_at TEXT,
  PRIMARY KEY (scope, path, hunk_id)
);
CREATE INDEX IF NOT EXISTS bs_scope ON block_state(scope);
CREATE TABLE IF NOT EXISTS prefs (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def default_db_path() -> str:
    state_home = os.environ.get("XDG_STATE_HOME",
                                os.path.expanduser("~/.local/state"))
    d = os.path.join(state_home, "seendiff")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "state.db")


def scope_key(repo: str, base_ref: str, branch: str | None, mb_sha: str) -> str:
    # symbolic on purpose: survives rebases (DEVELOPMENT.md, "Review state")
    return f"{repo}|{base_ref}|{branch or mb_sha}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_db(path: str | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(path or default_db_path(), check_same_thread=False)
    db.executescript(_SCHEMA)
    prune(db)
    return db


def prune(db: sqlite3.Connection, days: int = PRUNE_DAYS) -> None:
    cutoff = datetime.fromtimestamp(
        time.time() - days * 86400, timezone.utc).isoformat(timespec="seconds")
    with db:
        db.execute(
            "DELETE FROM block_state WHERE COALESCE(reviewed_at, '') < ? "
            "AND COALESCE(seen_at, '') < ?", (cutoff, cutoff))


def get_states(db: sqlite3.Connection, scope: str) -> dict[tuple[str, str], tuple[str | None, str | None]]:
    cur = db.execute(
        "SELECT path, hunk_id, seen_at, reviewed_at FROM block_state WHERE scope=?",
        (scope,))
    return {(p, h): (s, r) for p, h, s, r in cur}


def mark_seen(db: sqlite3.Connection, scope: str, blocks: list[tuple[str, str]]) -> None:
    now = _now()
    with db:
        db.executemany(
            "INSERT INTO block_state (scope, path, hunk_id, seen_at) VALUES (?,?,?,?) "
            "ON CONFLICT(scope, path, hunk_id) DO UPDATE SET "
            "seen_at = COALESCE(seen_at, excluded.seen_at)",
            [(scope, p, h, now) for p, h in blocks])


def mark_reviewed(db: sqlite3.Connection, scope: str, blocks: list[tuple[str, str]]) -> None:
    now = _now()
    with db:
        db.executemany(
            "INSERT INTO block_state (scope, path, hunk_id, seen_at, reviewed_at) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(scope, path, hunk_id) DO UPDATE SET "
            "seen_at = COALESCE(seen_at, excluded.seen_at), "
            "reviewed_at = excluded.reviewed_at",
            [(scope, p, h, now, now) for p, h in blocks])


def unmark_reviewed(db: sqlite3.Connection, scope: str,
                    blocks: list[tuple[str, str]] | None = None,
                    path: str | None = None,
                    all_: bool = False) -> None:
    """Demote to seen (reviewed_at -> NULL); seen_at stays."""
    with db:
        if all_:
            db.execute("UPDATE block_state SET reviewed_at=NULL WHERE scope=?",
                       (scope,))
        elif path is not None:
            db.execute(
                "UPDATE block_state SET reviewed_at=NULL WHERE scope=? AND path=?",
                (scope, path))
        elif blocks:
            db.executemany(
                "UPDATE block_state SET reviewed_at=NULL "
                "WHERE scope=? AND path=? AND hunk_id=?",
                [(scope, p, h) for p, h in blocks])


def get_prefs(db: sqlite3.Connection) -> dict[str, str]:
    """UI prefs live in the DB, not localStorage: each run picks a fresh port,
    and localStorage is keyed by origin, so port-local storage forgets."""
    return {k: v for k, v in db.execute("SELECT key, value FROM prefs")}


def set_pref(db: sqlite3.Connection, key: str, value: str) -> None:
    with db:
        db.execute(
            "INSERT INTO prefs (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))


def clear_scope(db: sqlite3.Connection, scope: str) -> None:
    with db:
        db.execute("DELETE FROM block_state WHERE scope=?", (scope,))
