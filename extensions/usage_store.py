"""Small, independent SQLite store. No upstream schema or migration changes."""

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

RETENTION_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    id INTEGER PRIMARY KEY CHECK (id = 1), started_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS visitors (
    user_id INTEGER PRIMARY KEY,
    first_seen REAL NOT NULL, last_seen REAL NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    successes INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    interrupted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS visitor_profiles (
    user_id INTEGER PRIMARY KEY REFERENCES visitors(user_id),
    display_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES visitors(user_id),
    chat_id INTEGER,
    kind TEXT NOT NULL, platform TEXT NOT NULL,
    started_at REAL NOT NULL, finished_at REAL,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failure', 'interrupted'))
);
CREATE INDEX IF NOT EXISTS requests_user_time ON requests(user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS requests_time ON requests(started_at);
CREATE INDEX IF NOT EXISTS visitors_last_seen ON visitors(last_seen);
"""


class UsageStore:
    """Synchronous short transactions; callers run these in asyncio.to_thread.

    A single bot process owns this store. Pending tasks from its previous run
    become interrupted, never successful. Only recent request details expire;
    visitor counts and lifetime totals remain.
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._initialized = False
        self._next_cleanup = 0.0
        self._started_at = time.time()

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=1)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA foreign_keys=ON")
                if not self._initialized:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.executescript(SCHEMA)
                    with conn:
                        conn.execute("INSERT OR IGNORE INTO metadata VALUES (1, ?)", (self._started_at,))
                        conn.execute(
                            "UPDATE visitors SET interrupted = interrupted + "
                            "(SELECT COUNT(*) FROM requests WHERE user_id = visitors.user_id AND status = 'running')"
                        )
                        conn.execute(
                            "UPDATE requests SET status = 'interrupted', finished_at = ? WHERE status = 'running'",
                            (time.time(),),
                        )
                    self._initialized = True
                with conn:
                    now = time.time()
                    cleanup_due = now >= self._next_cleanup
                    if cleanup_due:
                        conn.execute(
                            "DELETE FROM requests WHERE started_at < ? AND status != 'running'",
                            (now - RETENTION_DAYS * 86400,),
                        )
                    yield conn
                if cleanup_due:
                    self._next_cleanup = now + 3600
            finally:
                conn.close()

    @staticmethod
    def _touch(conn: sqlite3.Connection, user_id: int, now: float, display_name: str | None = None) -> None:
        conn.execute(
            "INSERT INTO visitors(user_id, first_seen, last_seen) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "first_seen = MIN(first_seen, excluded.first_seen), last_seen = MAX(last_seen, excluded.last_seen)",
            (user_id, now, now),
        )
        if display_name:
            conn.execute(
                "INSERT INTO visitor_profiles(user_id, display_name) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET display_name = excluded.display_name",
                (user_id, display_name),
            )

    def touch(self, user_id: int, display_name: str | None = None) -> None:
        with self._db() as conn:
            self._touch(conn, user_id, time.time(), display_name)

    def cache_names(self, names: dict[int, str]) -> None:
        """Fill existing contacts' missing names without manufacturing user activity."""
        with self._db() as conn:
            conn.executemany(
                "INSERT INTO visitor_profiles(user_id, display_name) "
                "SELECT user_id, ? FROM visitors WHERE user_id = ? "
                "ON CONFLICT(user_id) DO NOTHING",
                [(name, user_id) for user_id, name in names.items() if name],
            )

    def start_request(
        self, user_id: int, chat_id: int | None, kind: str, platform: str, display_name: str | None = None,
    ) -> int:
        with self._db() as conn:
            now = time.time()
            self._touch(conn, user_id, now, display_name)
            cursor = conn.execute(
                "INSERT INTO requests(user_id, chat_id, kind, platform, started_at, status) "
                "VALUES (?, ?, ?, ?, ?, 'running')",
                (user_id, chat_id, kind, platform, now),
            )
            conn.execute("UPDATE visitors SET requests = requests + 1 WHERE user_id = ?", (user_id,))
            assert cursor.lastrowid is not None
            return cursor.lastrowid

    def finish_request(self, request_id: int, status: str) -> None:
        counter = {"success": "successes", "failure": "failures", "interrupted": "interrupted"}[status]
        with self._db() as conn:
            row = conn.execute(
                "UPDATE requests SET status = ?, finished_at = ? WHERE id = ? AND status = 'running' "
                "RETURNING user_id",
                (status, time.time(), request_id),
            ).fetchone()
            if row:
                # counter is selected from a fixed mapping, never user input.
                conn.execute(f"UPDATE visitors SET {counter} = {counter} + 1 WHERE user_id = ?", (row[0],))

    def summary(self, today: float, week_start: float) -> dict[str, Any]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS users, COALESCE(SUM(last_seen >= ?), 0) AS today_users, "
                "COALESCE(SUM(last_seen >= ?), 0) AS week_users, "
                "COALESCE(SUM(requests), 0) AS requests, COALESCE(SUM(successes), 0) AS successes, "
                "COALESCE(SUM(failures), 0) AS failures, COALESCE(SUM(interrupted), 0) AS interrupted "
                "FROM visitors",
                (today, week_start),
            ).fetchone()
            result = dict(row)
            result["started_at"] = conn.execute("SELECT started_at FROM metadata WHERE id = 1").fetchone()[0]
            result["today_requests"] = dict(
                conn.execute(
                    "SELECT status, COUNT(*) FROM requests WHERE started_at >= ? GROUP BY status", (today,)
                ).fetchall()
            )
            return result

    def user_report(self, user_id: int) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT visitors.*, visitor_profiles.display_name FROM visitors "
                "LEFT JOIN visitor_profiles USING (user_id) WHERE user_id = ?", (user_id,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["recent"] = [
                dict(event)
                for event in conn.execute(
                    "SELECT kind, platform, started_at, status FROM requests "
                    "WHERE user_id = ? ORDER BY started_at DESC, id DESC LIMIT 10",
                    (user_id,),
                )
            ]
            return result

    def users_page(self, page: int = 0) -> dict[str, Any]:
        """Recent contacts, ten per page; clamp stale or out-of-range pages."""
        with self._db() as conn:
            total = conn.execute("SELECT COUNT(*) FROM visitors").fetchone()[0]
            pages = max(1, (total + 9) // 10)
            page = min(max(0, page), pages - 1)
            users = [
                dict(row)
                for row in conn.execute(
                    "SELECT user_id, last_seen, requests, display_name FROM visitors "
                    "LEFT JOIN visitor_profiles USING (user_id) "
                    "ORDER BY last_seen DESC, user_id DESC LIMIT 10 OFFSET ?",
                    (page * 10,),
                )
            ]
            return {"users": users, "total": total, "page": page, "pages": pages}
