"""Public tests for the synthetic GTD Engine showcase."""
from __future__ import annotations

import sys
import subprocess
import json
import os
import tempfile
import unittest
import atexit
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_HOME = Path(tempfile.mkdtemp(prefix="gtd-engine-tests-"))
os.environ["GTD_HOME"] = str(TEST_HOME)
atexit.register(lambda: __import__("shutil").rmtree(TEST_HOME, ignore_errors=True))
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

    def test_cli_rejects_missing_dependency_and_reports_saved_changes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gtd-cli-") as temporary:
            env = dict(os.environ, GTD_HOME=temporary)
            env.pop("GTD_DB", None)
            def run(*args):
                return subprocess.run([sys.executable, "-B", "-m", "gtd.cli", "--json", *args],
                                      cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(run("init").returncode, 0)
            missing = run("action", "add", "Invalid dependency", "--after", "999")
            self.assertEqual(missing.returncode, 2)
            self.assertEqual(json.loads(run("next").stdout), [])
            Path(temporary, "history.jsonl").mkdir()
            captured = run("capture", "User task")
            self.assertEqual(captured.returncode, 0, captured.stderr)
            self.assertIn("database change saved", captured.stderr)
            iid = json.loads(captured.stdout)["inbox_item"]
            clarified = run("clarify", str(iid), "--as", "action", "--text", "First action")
            self.assertEqual(clarified.returncode, 0, clarified.stderr)
            self.assertIn("database change saved", clarified.stderr)
            aid = json.loads(clarified.stdout)["id"]
            added = run("action", "add", "Second action", "--after", str(aid))
            self.assertEqual(added.returncode, 0, added.stderr)
            self.assertIn("database change saved", added.stderr)
            done = run("done", str(aid))
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("database change saved", done.stderr)
            self.assertEqual([row["text"] for row in json.loads(run("next").stdout)], ["Second action"])

    def test_invalid_state_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gtd-engine-showcase-") as temporary:
            conn = db.connect(Path(temporary) / "demo.db")
            migrations.migrate(conn)
            with self.assertRaises(mutations.GtdError):
                mutations.add_action(conn, "Invalid", commitment_type="urgent")
            conn.close()

    def test_installed_daily_action_and_project_workflow(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gtd-daily-cli-") as temporary:
            env = dict(os.environ, GTD_HOME=temporary)
            env.pop("GTD_DB", None)
            def run(*args):
                return subprocess.run([sys.executable, "-B", "-m", "gtd.cli", "--json", *args],
                                      cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(0, run("init").returncode)
            first = json.loads(run("action", "add", "Draft proposal").stdout)["action"]
            second = json.loads(run("action", "add", "Review proposal", "--after", str(first)).stdout)["action"]
            self.assertEqual(0, run("done", str(first)).returncode)
            self.assertEqual(["Review proposal"], [row["text"] for row in json.loads(run("next").stdout)])
            reopened = run("action", "reopen", str(first))
            self.assertEqual(0, reopened.returncode, reopened.stderr)
            self.assertEqual(["Draft proposal"], [row["text"] for row in json.loads(run("next").stdout)])
            self.assertEqual(["Review proposal"], [row["text"] for row in json.loads(run("blocked").stdout)])
            edited = run("action", "edit", str(first), "--text", "Draft the proposal", "--reason", "Clarify wording")
            self.assertEqual(0, edited.returncode, edited.stderr)
            shown = json.loads(run("show", "action", str(first)).stdout)
            self.assertEqual("Draft the proposal", shown["text"])
            self.assertEqual("Clarify wording", shown["corrections"][-1]["reason"])
            deferred = run("action", "defer", str(first), "--until", "2099-01-01")
            self.assertEqual(0, deferred.returncode, deferred.stderr)
            self.assertEqual([], json.loads(run("next").stdout))
            undelayed = run("action", "undelay", str(first))
            self.assertEqual(0, undelayed.returncode, undelayed.stderr)
            self.assertEqual(1, json.loads(undelayed.stdout)["removed_date_blocks"])
            history = json.loads(run("history").stdout)
            self.assertEqual(["defer", "undelay"], [item["event"] for item in history[-2:]])
            self.assertEqual(["Draft the proposal"], [row["text"] for row in json.loads(run("next").stdout)])
            self.assertEqual(2, run("action", "defer", str(first), "--until", "not-a-date").returncode)
            self.assertEqual(2, run("action", "park", "999").returncode)
            project = json.loads(run("project", "add", "Proposal is accepted").stdout)["project"]
            self.assertEqual(["Proposal is accepted"], [row["outcome"] for row in json.loads(run("project", "list").stdout)])
            child = json.loads(run("action", "add", "Send proposal", "--project", str(project)).stdout)["action"]
            refused = run("project", "close", str(project), "--state", "done")
            self.assertEqual(2, refused.returncode)
            self.assertIn(str(child), refused.stderr)
            self.assertEqual("active", json.loads(run("show", "project", str(project)).stdout)["state"])
            self.assertEqual([child], [item["id"] for item in json.loads(run("project", "actions", str(project)).stdout)])
            self.assertEqual(0, run("done", str(child)).returncode)
            closed = run("project", "close", str(project), "--state", "done")
            self.assertEqual(0, closed.returncode, closed.stderr)
            self.assertEqual("done", json.loads(run("show", "project", str(project)).stdout)["state"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
