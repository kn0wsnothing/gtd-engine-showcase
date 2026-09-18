"""Process-local journal adapter for the temporary showcase database.

The private engine writes an external durable journal after selected database transactions.
This adapter records the same calls only in process memory. Its failure switch is deliberate:
it lets the test prove that a journal error is reported after the database transition.
"""
from __future__ import annotations

import os

EVENTS: list[tuple[str, tuple, dict]] = []


def _record(kind: str, *args, **kwargs) -> None:
    if os.environ.get("GTD_SHOWCASE_JOURNAL_FAIL") == "1":
        raise OSError("synthetic journal failure")
    EVENTS.append((kind, args, kwargs))


def capture(*args, **kwargs): _record("capture", *args, **kwargs)
def clarification(*args, **kwargs): _record("clarification", *args, **kwargs)
def added(*args, **kwargs): _record("added", *args, **kwargs)
def closure(*args, **kwargs): _record("closure", *args, **kwargs)
def correction(*args, **kwargs): _record("correction", *args, **kwargs)
