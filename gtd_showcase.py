#!/usr/bin/env python3
"""Run a synthetic GTD Engine state transition using a temporary SQLite database."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

# This sample can run beside an editable private checkout. Make the candidate, rather than an
# installed package, the only possible source for its engine modules.
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from gtd import db, migrations, mutations, queries


def _assert_local_engine() -> None:
    for name, module in sys.modules.items():
        if name == "gtd" or name.startswith("gtd."):
            source = getattr(module, "__file__", None)
            if not source or ROOT not in Path(source).resolve().parents:
                raise RuntimeError(f"non-local engine module loaded: {name}")


def run() -> dict:
    with tempfile.TemporaryDirectory(prefix="gtd-engine-showcase-") as root:
        path = Path(root) / "demo.db"
        conn = db.connect(path)
        migrations.migrate(conn)
        inbox_id = mutations.capture(conn, "Draft the launch checklist", "synthetic-demo")
        first_id = mutations.clarify(
            conn, inbox_id, "action", text="Draft the launch checklist",
            commitment_type="obligation", work_type="closed", execution_type="interactive",
        )
        dependent_id = mutations.add_action(
            conn, "Review the launch checklist", commitment_type="obligation",
            work_type="closed", execution_type="interactive",
            blocks=[{"kind": "done", "ref_kind": "action", "ref_id": first_id}],
        )
        before = [item["text"] for item in queries.next_actions(conn)]
        completion = mutations.done(conn, first_id)
        after = [item["text"] for item in queries.next_actions(conn)]
        conn.close()
    return {
        "capture_clarified_as": first_id > 0,
        "blocked_before_completion": "Review the launch checklist" not in before,
        "completion_unblocked": [item["text"] for item in completion["unblocked"]],
        "next_after_completion": after,
        "dependent_action_created": dependent_id > 0,
    }


def invalid_state() -> dict:
    with tempfile.TemporaryDirectory(prefix="gtd-engine-showcase-") as root:
        conn = db.connect(Path(root) / "demo.db")
        migrations.migrate(conn)
        try:
            mutations.add_action(conn, "Bad state", commitment_type="urgent")
        except mutations.GtdError as exc:
            return {"rejected": str(exc)}
        finally:
            conn.close()
    raise AssertionError("invalid state was accepted")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "invalid-state"))
    args = parser.parse_args(argv)
    _assert_local_engine()
    print(json.dumps(run() if args.command == "run" else invalid_state(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
