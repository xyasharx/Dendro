# dendro/core/capability_cache.py
"""Two-tier capability & provider cache (L1 RAM + L2 SQLite WAL)."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import weakref

LOGGER = logging.getLogger(__name__)


def dendro_cache_dir() -> str:
    """Return Dendro's per-user cache directory honoring ``XDG_CACHE_HOME``."""
    xdg_cache = (os.environ.get("XDG_CACHE_HOME") or "").strip()
    cache_root = (
        xdg_cache
        if xdg_cache and os.path.isabs(xdg_cache)
        else os.path.expanduser("~/.cache")
    )
    return os.path.join(cache_root, "dendro")


def _close_tracked_connections(
    connections: dict[int, sqlite3.Connection], lock: threading.Lock
) -> None:
    """Close any remaining SQLite connections when a cache instance is collected."""
    with lock:
        pending = list(connections.values())
        connections.clear()
    for conn in pending:
        try:
            conn.close()
        except sqlite3.Error:
            LOGGER.debug(
                "Could not close capability-cache connection during cleanup.",
                exc_info=True,
            )


class SQLiteCapabilityCache:
    """Thread-safe L1 cache with an optional, failure-contained SQLite L2 cache."""

    _instance: SQLiteCapabilityCache | None = None
    _lock = threading.Lock()

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self._memory_cache: dict[str, tuple[bool, str]] = {}
        self._local_storage = threading.local()
        self._connections: dict[int, sqlite3.Connection] = {}
        self._conn_lock = threading.Lock()
        self._finalizer = weakref.finalize(
            self, _close_tracked_connections, self._connections, self._conn_lock
        )
        self._disk_cache_enabled = True
        self.db_path = ""
        try:
            if db_path is not None:
                resolved = os.fspath(db_path)
                os.makedirs(os.path.dirname(resolved) or ".", exist_ok=True)
                self.db_path = resolved
            else:
                cache_dir = dendro_cache_dir()
                os.makedirs(cache_dir, exist_ok=True)
                # v6 starts a clean generation: v5 could persist a missing provider
                # as satisfied, so carrying that result into a corrected build would
                # still mislabel dependencies until a manual cache deletion.
                self.db_path = os.path.join(cache_dir, "capabilities_v6.db")
            self._init_db()
        except (OSError, sqlite3.Error):
            # Dependency inspection remains correct without persistence. A
            # read-only home, corrupt cache, or unavailable filesystem must not
            # crash the UI while constructing a worker.
            self._disk_cache_enabled = False
            self.close()
            LOGGER.warning(
                "Capability cache persistence is unavailable; using the in-memory cache only.",
                exc_info=True,
            )

    @classmethod
    def get_instance(cls) -> SQLiteCapabilityCache:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def close_instance(cls) -> None:
        """Close the active singleton cache's SQLite connections if initialized."""
        with cls._lock:
            instance = cls._instance
        if instance is not None:
            instance.close()

    def _get_connection(self) -> sqlite3.Connection:
        if not self._disk_cache_enabled or not self.db_path:
            raise sqlite3.OperationalError("capability cache persistence is disabled")
        thread_id = threading.get_ident()
        conn = getattr(self._local_storage, "conn", None)
        if conn is not None:
            with self._conn_lock:
                if self._connections.get(thread_id) is conn:
                    return conn
            # Connection was closed via invalidate()/close() from another thread.
            self._local_storage.conn = None

        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        self._local_storage.conn = conn
        with self._conn_lock:
            self._connections[thread_id] = conn
        return conn

    def _init_db(self) -> None:
        conn = self._get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS capabilities (
                cap_name TEXT PRIMARY KEY,
                is_satisfied INTEGER,
                provider_name TEXT
            )
        """)
        conn.commit()

    def close(self) -> None:
        """Close all open thread-local SQLite connections cleanly."""
        self._local_storage.conn = None
        with self._conn_lock:
            connections = list(self._connections.values())
            self._connections.clear()
        for connection in connections:
            try:
                connection.close()
            except sqlite3.Error:
                LOGGER.warning(
                    "Could not close capability-cache connection.",
                    exc_info=True,
                )

    def _disable_disk_cache(self, operation: str) -> None:
        """Switch permanently to L1 after a disk-cache failure in this process."""
        with self._lock:
            was_enabled = self._disk_cache_enabled
            self._disk_cache_enabled = False
        self.close()
        if was_enabled:
            LOGGER.warning(
                "Capability cache %s failed; disabling persistent caching for this process.",
                operation,
                exc_info=True,
            )

    def get(self, cap_name: str) -> tuple[bool, str] | None:
        with self._lock:
            if cap_name in self._memory_cache:
                return self._memory_cache[cap_name]
            disk_cache_enabled = self._disk_cache_enabled
        if not disk_cache_enabled:
            return None

        try:
            conn = self._get_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT is_satisfied, provider_name FROM capabilities WHERE cap_name = ?",
                (cap_name,),
            )
            row = cur.fetchone()
            if row:
                raw_satisfied, raw_provider = row
                if (
                    not isinstance(raw_satisfied, int)
                    or isinstance(raw_satisfied, bool)
                    or raw_satisfied not in {0, 1}
                    or not isinstance(raw_provider, str)
                    or not raw_provider
                ):
                    raise sqlite3.DatabaseError(
                        "capability cache contains an invalid provider record"
                    )
                result = (bool(raw_satisfied), raw_provider)
                with self._lock:
                    self._memory_cache[cap_name] = result
                return result
        except (OSError, sqlite3.Error):
            self._disable_disk_cache("read")
        return None

    def set_batch(self, items: list[tuple[str, bool, str]]) -> None:
        if not items:
            return
        with self._lock:
            for name, satisfied, provider in items:
                self._memory_cache[name] = (satisfied, provider)
            disk_cache_enabled = self._disk_cache_enabled
        if not disk_cache_enabled:
            return

        try:
            conn = self._get_connection()
            conn.executemany(
                "INSERT OR REPLACE INTO capabilities (cap_name, is_satisfied, provider_name) VALUES (?, ?, ?)",
                [
                    (name, int(satisfied), provider)
                    for name, satisfied, provider in items
                ],
            )
            conn.commit()
        except (OSError, sqlite3.Error):
            self._disable_disk_cache("write")

    def invalidate(self) -> None:
        """Discard provider facts and release SQLite handles after a transaction."""
        with self._lock:
            self._memory_cache.clear()
            disk_cache_enabled = self._disk_cache_enabled
        if not disk_cache_enabled:
            self.close()
            return

        try:
            conn = self._get_connection()
            conn.execute("DELETE FROM capabilities")
            conn.commit()
        except (OSError, sqlite3.Error):
            self._disable_disk_cache("invalidation")
            return
        self.close()


__all__ = ("SQLiteCapabilityCache", "dendro_cache_dir")
