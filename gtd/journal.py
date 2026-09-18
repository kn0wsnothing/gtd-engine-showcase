"""Durable, user-local history for the public GTD application."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from . import config


def _record(kind: str, *args, **kwargs) -> None:
    event = {"at": datetime.now(timezone.utc).isoformat(), "event": kind,
             "args": list(args), "fields": kwargs}
    try:
        config.HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with config.HISTORY_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, default=str) + "\n")
    except OSError:
        print("warning: database change saved; history could not be appended. "
              "Do not repeat the mutation. Check the history path and permissions.", file=sys.stderr)


def capture(*args, **kwargs): _record("capture", *args, **kwargs)
def clarification(*args, **kwargs): _record("clarification", *args, **kwargs)
def added(*args, **kwargs): _record("added", *args, **kwargs)
def closure(*args, **kwargs): _record("closure", *args, **kwargs)
def correction(*args, **kwargs): _record("correction", *args, **kwargs)
