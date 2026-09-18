"""Connection management.

The single writer is *logical*, not process-level: an interactive session, a draining
harness job, and two deterministic crons can all hit the file in the same second. WAL mode
plus a real busy_timeout plus one transaction per mutation makes that contention a
non-event rather than a SQLITE_BUSY failure.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config

BUSY_TIMEOUT_MS = 10_000


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else config.DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    """One transaction per mutation.

    Transaction-time propagation rides *inside* the same transaction as the write that
    triggered it, so an unblock is never visible without the completion that caused it.
    IMMEDIATE takes the write lock up front so two writers queue rather than deadlock on
    upgrade.

    Reentrant, because some mutations are composed of others. `clarify` creating an action and
    the agent handoff that suppresses it has to be one transaction — an action written without
    its handoff is exactly the `automated-unattached` defect, and two sequential transactions
    can leave that state behind. A nested `BEGIN` is an error in SQLite, so an inner block
    joins the transaction already open instead of starting its own, and the outer block's
    rollback covers everything either of them wrote.
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def today() -> str:
    return date.today().isoformat()


def iso_week(d: date | None = None) -> str:
    """Current ISO week as YYYY-Www — the format the legacy [commit::] tag used."""
    iso = (d or date.today()).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def business_days_between(start: date, end: date) -> int:
    """Weekdays elapsed from `start` to `end`, weekends never counted.

    A work-typed record's staleness clock stands still over Saturday and Sunday — work
    day-job obligations are not neglected by sitting through a weekend the operator does not work.
    Personal records keep plain calendar days instead: weekends are exactly when personal
    deep work happens, so they should count like any other day.
    """
    if end <= start:
        return 0
    days = 0
    d = start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days
