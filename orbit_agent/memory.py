from __future__ import annotations

import sqlite3
import json
import time
import logging
from pathlib import Path
from typing import Any, Dict, List
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# Database path
DB_DIR = Path.home() / ".orbit"
DB_DIR.mkdir(exist_ok=True)
DB_PATH = DB_DIR / "orbit.db"


def _init_db():
    """Initialize the SQLite database with required tables"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS advice_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                kind TEXT NOT NULL,
                history TEXT NOT NULL,
                advice TEXT NOT NULL,
                actions_48h TEXT NOT NULL,
                metric_to_watch TEXT,
                risks TEXT NOT NULL,
                score INTEGER,
                critique TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_advice_timestamp
            ON advice_log(timestamp)
        """)
        conn.commit()


@contextmanager
def get_db_connection():
    """Get a database connection with proper error handling"""
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        yield conn
    except sqlite3.Error as e:
        logger.error(f"Database error: {e}")
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def append_entry(kind: str, payload: Dict[str, Any]) -> None:
    """Append an entry to the advice log with thread safety"""
    try:
        _init_db()

        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO advice_log (
                    timestamp, kind, history, advice, actions_48h,
                    metric_to_watch, risks, score, critique
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    int(time.time()),
                    kind,
                    json.dumps(payload.get("history", [])),
                    payload.get("advice", ""),
                    json.dumps(payload.get("actions_48h", [])),
                    payload.get("metric_to_watch", ""),
                    json.dumps(payload.get("risks", [])),
                    payload.get("score", 0),
                    payload.get("critique", ""),
                ),
            )
            conn.commit()

        logger.info(f"Logged {kind} entry to database")

    except Exception as e:
        logger.error(f"Failed to append entry: {e}")
        # Fallback to file-based logging if DB fails
        _append_entry_fallback(kind, payload)


def _append_entry_fallback(kind: str, payload: Dict[str, Any]) -> None:
    """Fallback to JSON file if database fails"""
    fallback_path = DB_DIR / "ledger_fallback.json"

    try:
        data = []
        if fallback_path.exists():
            with open(fallback_path) as f:
                data = json.load(f)

        entry = {"ts": int(time.time()), "kind": kind, **payload}
        data.append(entry)

        with open(fallback_path, "w") as f:
            json.dump(data, f, indent=2)

        logger.info("Used fallback JSON logging")

    except Exception as e:
        logger.error(f"Even fallback logging failed: {e}")


def get_recent_advice(limit: int = 10) -> List[Dict[str, Any]]:
    """Get recent advice entries from the database"""
    try:
        _init_db()

        with get_db_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM advice_log
                WHERE kind = 'advice'
                ORDER BY timestamp DESC
                LIMIT ?
            """,
                (limit,),
            )

            results = []
            for row in cursor:
                results.append(
                    {
                        "timestamp": row["timestamp"],
                        "history": json.loads(row["history"]),
                        "advice": row["advice"],
                        "actions_48h": json.loads(row["actions_48h"]),
                        "metric_to_watch": row["metric_to_watch"],
                        "risks": json.loads(row["risks"]),
                        "score": row["score"],
                        "critique": row["critique"],
                    }
                )

            return results

    except Exception as e:
        logger.error(f"Failed to get recent advice: {e}")
        return []


def load_context() -> str:
    """Load user context from file"""
    ctx_path = DB_DIR / "context.md"

    if not ctx_path.exists():
        return ""

    try:
        return ctx_path.read_text()
    except Exception as e:
        logger.error(f"Failed to load context: {e}")
        return ""


def save_context(content: str) -> bool:
    """Save user context to file"""
    ctx_path = DB_DIR / "context.md"

    try:
        ctx_path.write_text(content)
        logger.info("Context saved successfully")
        return True
    except Exception as e:
        logger.error(f"Failed to save context: {e}")
        return False


def cleanup_old_entries(days: int = 90) -> int:
    """Clean up entries older than specified days"""
    try:
        _init_db()
        cutoff = int(time.time()) - (days * 24 * 60 * 60)

        with get_db_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM advice_log WHERE timestamp < ?", (cutoff,)
            )
            deleted = cursor.rowcount
            conn.commit()

        logger.info(f"Cleaned up {deleted} old entries")
        return deleted

    except Exception as e:
        logger.error(f"Failed to cleanup entries: {e}")
        return 0
