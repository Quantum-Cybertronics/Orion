"""Keeps the SQLite file from growing forever.

SQLite never shrinks its file when rows are deleted; the freed pages are kept
for reuse. ``VACUUM`` rewrites the file without them. It is slow on a big
database and needs the file to itself, so ORION does it lazily: a few seconds
after a deletion (once the request is over) and again at shutdown, and only if
enough space would actually be given back.
"""

import logging
import os
import threading

from sqlalchemy.engine import Engine

logger = logging.getLogger("orion.storage")

# Don't rewrite the whole file to recover less than this.
VACUUM_MIN_FREE_BYTES = 256 * 1024
VACUUM_DELAY_SECONDS = 3.0


def auto_vacuum_enabled() -> bool:
    return os.getenv("ORION_AUTO_VACUUM", "1").strip().lower() not in ("0", "false", "no")


def reclaimable_bytes(engine: Engine) -> int:
    """Space inside the database file that deleted rows left behind."""
    if engine.dialect.name != "sqlite":
        return 0

    with engine.connect() as connection:
        page_size = connection.exec_driver_sql("PRAGMA page_size").scalar() or 0
        free_pages = connection.exec_driver_sql("PRAGMA freelist_count").scalar() or 0

    return int(page_size) * int(free_pages)


def vacuum_if_worthwhile(
    engine: Engine,
    min_free_bytes: int = VACUUM_MIN_FREE_BYTES,
) -> bool:
    """Shrink the database file if at least ``min_free_bytes`` can be recovered."""
    if reclaimable_bytes(engine) < min_free_bytes:
        return False

    # VACUUM cannot run inside a transaction.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql("VACUUM")

    return True


class VacuumScheduler:
    """Runs at most one delayed vacuum at a time, off the request thread."""

    def __init__(self, delay: float = VACUUM_DELAY_SECONDS):
        self.delay = delay
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def request(self, engine: Engine) -> None:
        if not auto_vacuum_enabled():
            return

        with self._lock:
            if self._timer is not None:
                return  # one is already waiting; it will cover this deletion too

            self._timer = threading.Timer(self.delay, self._run, args=(engine,))
            self._timer.daemon = True
            self._timer.start()

    def _run(self, engine: Engine) -> None:
        with self._lock:
            self._timer = None

        try:
            if vacuum_if_worthwhile(engine):
                logger.info("Database compacted.")
        except Exception:
            # Usually "database is locked": try again after the next deletion.
            logger.warning("Could not compact the database.", exc_info=True)

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


scheduler = VacuumScheduler()


def request_vacuum(engine: Engine) -> None:
    scheduler.request(engine)


def compact_on_shutdown(engine: Engine) -> None:
    """Final tidy-up when ORION exits (best effort; never blocks shutdown)."""
    scheduler.cancel()

    if not auto_vacuum_enabled():
        return

    try:
        vacuum_if_worthwhile(engine)
    except Exception:
        logger.warning("Could not compact the database at shutdown.", exc_info=True)
