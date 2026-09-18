"""Public tests for the synthetic GTD Engine showcase."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The suite is designed to run from an uninstalled candidate. Put only that candidate ahead of
# site packages, then assert the package origin below so an editable private checkout cannot mask it.
sys.path.insert(0, str(ROOT))

import gtd
from gtd import db, migrations, mutations, queries


class DemoTests(unittest.TestCase):
    def test_imports_are_local_and_transition_propagates(self) -> None:
        self.assertEqual(Path(gtd.__file__).resolve().parent, ROOT / "gtd")
        with tempfile.TemporaryDirectory(prefix="gtd-engine-showcase-") as temporary:
            conn = db.connect(Path(temporary) / "demo.db")
            migrations.migrate(conn)
            inbox = mutations.capture(conn, "Draft launch checklist", "synthetic-test")
            first = mutations.clarify(conn, inbox, "action", text="Draft launch checklist",
                                      commitment_type="obligation", work_type="closed",
                                      execution_type="interactive")
            second = mutations.add_action(conn, "Review launch checklist", commitment_type="obligation",
                                          blocks=[{"kind": "done", "ref_kind": "action", "ref_id": first}])
            self.assertNotIn("Review launch checklist", [row["text"] for row in queries.next_actions(conn)])
            self.assertEqual(mutations.done(conn, first)["unblocked"][0]["id"], second)
            self.assertIn("Review launch checklist", [row["text"] for row in queries.next_actions(conn)])
            conn.close()

    def test_invalid_state_is_rejected_and_journal_failure_surfaces(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gtd-engine-showcase-") as temporary:
            conn = db.connect(Path(temporary) / "demo.db")
            migrations.migrate(conn)
            with self.assertRaises(mutations.GtdError):
                mutations.add_action(conn, "Invalid", commitment_type="urgent")
            action = mutations.add_action(conn, "Journal failure", commitment_type="obligation")
            previous = os.environ.get("GTD_SHOWCASE_JOURNAL_FAIL")
            os.environ["GTD_SHOWCASE_JOURNAL_FAIL"] = "1"
            try:
                with self.assertRaises(OSError): mutations.done(conn, action)
            finally:
                if previous is None: os.environ.pop("GTD_SHOWCASE_JOURNAL_FAIL", None)
                else: os.environ["GTD_SHOWCASE_JOURNAL_FAIL"] = previous
            self.assertEqual(conn.execute("SELECT state FROM action WHERE id=?", (action,)).fetchone()["state"], "done")
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
