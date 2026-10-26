"""Persistent watchlists and Telegram cursor; a single process owns the database."""

from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3
import time
from typing import Iterator

MAX_WATCHLIST_SIZE = 20
CURSOR_IDLE_SECONDS = 6 * 24 * 60 * 60


class AlreadyRunning(RuntimeError):
    pass


class MarketChanged(RuntimeError):
    pass


@contextmanager
def instance_lock(database_path: Path) -> Iterator[None]:
    """Use an OS lock, released automatically even if the process is killed."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with database_path.with_suffix(database_path.suffix + ".lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise AlreadyRunning("Another process is using this database.") from None
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.close()
            raise RuntimeError("Unsupported database schema version.")
        with self.connection:
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS watchlist ("
                "user_id INTEGER NOT NULL, ticker TEXT NOT NULL, "
                "PRIMARY KEY (user_id, ticker))"
            )
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self.connection.execute("PRAGMA user_version=1")

    def close(self) -> None:
        self.connection.close()

    def list_tickers(self, user_id: int) -> list[str]:
        return [row[0] for row in self.connection.execute(
            "SELECT ticker FROM watchlist WHERE user_id=? ORDER BY ticker", (user_id,)
        )]

    def add(self, user_id: int, ticker: str) -> str:
        with self.connection:
            existing = self.list_tickers(user_id)
            if ticker in existing:
                return "exists"
            if len(existing) >= MAX_WATCHLIST_SIZE:
                return "full"
            self.connection.execute(
                "INSERT INTO watchlist(user_id,ticker) VALUES (?,?)", (user_id, ticker)
            )
        return "added"

    def remove(self, user_id: int, ticker: str) -> bool:
        with self.connection:
            result = self.connection.execute(
                "DELETE FROM watchlist WHERE user_id=? AND ticker=?", (user_id, ticker)
            )
        return result.rowcount > 0

    def delete_user(self, user_id: int) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM watchlist WHERE user_id=?", (user_id,))

    def bind_bot(self, bot_id: int) -> None:
        """Never reuse one bot's cursor/watchlists with a different bot token."""
        with self.connection:
            row = self.connection.execute(
                "SELECT value FROM metadata WHERE key='telegram_bot_id'"
            ).fetchone()
            if row and int(row[0]) != bot_id:
                raise RuntimeError("Database belongs to a different Telegram bot.")
            self.connection.execute(
                "INSERT OR IGNORE INTO metadata(key,value) VALUES ('telegram_bot_id',?)",
                (str(bot_id),),
            )

    def bind_market(self, market: str) -> None:
        """Do not silently reinterpret existing MOEX watchlists as US tickers."""
        with self.connection:
            row = self.connection.execute(
                "SELECT value FROM metadata WHERE key='market'"
            ).fetchone()
            has_watchlist = self.connection.execute("SELECT 1 FROM watchlist LIMIT 1").fetchone()
            if (row and row[0] != market) or (not row and has_watchlist):
                raise MarketChanged("Use a new database for the changed market provider.")
            self.connection.execute(
                "INSERT OR IGNORE INTO metadata(key,value) VALUES ('market',?)", (market,)
            )

    def get_offset(self, now: float | None = None) -> int:
        values = dict(self.connection.execute(
            "SELECT key,value FROM metadata WHERE key IN ('telegram_offset','offset_saved_at')"
        ))
        saved_at = float(values.get("offset_saved_at", "0"))
        current = time.time() if now is None else now
        # Telegram can choose a random update_id after a week of inactivity.
        # Probe without an old offset before that happens, including in a long-lived process.
        if current - saved_at >= CURSOR_IDLE_SECONDS:
            return 0
        return int(values.get("telegram_offset", "0"))

    def set_offset(self, value: int, now: float | None = None) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO metadata(key,value) VALUES ('telegram_offset',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(value),)
            )
            self.connection.execute(
                "INSERT INTO metadata(key,value) VALUES ('offset_saved_at',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(time.time() if now is None else now),),
            )
