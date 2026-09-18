"""A local, persistent command-line GTD application."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

from . import config, db, journal, migrations, mutations, queries


def conn():
    connection = db.connect(config.DB_PATH)
    migrations.migrate(connection)
    return connection


def emit(value, json_out: bool) -> None:
    if json_out:
        print(json.dumps(value, indent=2, sort_keys=True, default=str))
    elif isinstance(value, list):
        for row in value:
            print(f"{row.get('id')}: {row.get('text') or row.get('outcome') or row.get('raw')}")
    elif isinstance(value, dict):
        for key, item in value.items(): print(f"{key}: {item}")
    else: print(value)


def require_iso_date(value: str) -> str:
    """Accept only an ISO calendar date for a date block."""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from error


def require_record(connection, table: str, record_id: int) -> None:
    if connection.execute(f"SELECT id FROM {table} WHERE id=?", (record_id,)).fetchone() is None:
        label = "project" if table == "project" else "action"
        raise mutations.GtdError(f"{label} #{record_id} not found")


def undelay(connection, action_id: int) -> dict[str, object]:
    """Remove this action's date blocks while leaving other blocks intact."""
    require_record(connection, "action", action_id)
    with db.tx(connection):
        dates = [row["ref_date"] for row in connection.execute(
            "SELECT ref_date FROM block WHERE action_id=? AND kind='date_reached'", (action_id,)
        )]
        result = connection.execute(
            "DELETE FROM block WHERE action_id=? AND kind='date_reached'", (action_id,)
        )
    journal._record("undelay", action=action_id, dates=dates, removed_count=result.rowcount)
    return {"removed_date_blocks": result.rowcount, "dates": dates}


def defer(connection, action_id: int, until: str) -> int:
    require_record(connection, "action", action_id)
    block = mutations.defer(connection, action_id, until)
    journal._record("defer", action=action_id, date=until, block=block)
    return block


def close_project(connection, project_id: int, state: str) -> dict:
    require_record(connection, "project", project_id)
    unfinished = [row["id"] for row in connection.execute(
        "SELECT id FROM action WHERE project_id=? AND state IN ('open','someday') ORDER BY id", (project_id,)
    )]
    if unfinished:
        ids = ", ".join(str(item) for item in unfinished)
        raise mutations.GtdError(
            f"project #{project_id} has unfinished actions: {ids}. "
            "Use done, drop, or reassign each action before closing the project."
        )
    return mutations.close_project(connection, project_id, state)


def project_actions(connection, project_id: int) -> list[dict]:
    require_record(connection, "project", project_id)
    return [dict(row) for row in connection.execute(
        "SELECT id, text, state FROM action WHERE project_id=? ORDER BY id", (project_id,)
    )]


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gtd", description=__doc__)
    p.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create or open the local database; never erases data")
    c = sub.add_parser("capture"); c.add_argument("text"); c.add_argument("--provenance", default="manual")
    i = sub.add_parser("inbox"); i.add_argument("--state")
    cl = sub.add_parser("clarify"); cl.add_argument("id", type=int); cl.add_argument("--as", dest="kind", choices=("action", "project", "nothing"), required=True); cl.add_argument("--text"); cl.add_argument("--project", type=int); cl.add_argument("--reason", default="")
    project = sub.add_parser("project"); project_sub = project.add_subparsers(dest="project_command", required=True)
    pa = project_sub.add_parser("add"); pa.add_argument("outcome"); pa.add_argument("--done-test")
    pl = project_sub.add_parser("list"); pl.add_argument("--state", choices=("active", "someday", "on-hold", "done", "dropped", "demoted"), default="active")
    pc = project_sub.add_parser("close"); pc.add_argument("id", type=int); pc.add_argument("--state", choices=("done", "dropped"), default="done")
    pactions = project_sub.add_parser("actions"); pactions.add_argument("id", type=int)
    action = sub.add_parser("action"); action_sub = action.add_subparsers(dest="action_command", required=True)
    aa = action_sub.add_parser("add"); aa.add_argument("text"); aa.add_argument("--project", type=int); aa.add_argument("--after", type=int); aa.add_argument("--type", choices=config.COMMITMENT_TYPES, default="obligation")
    ad = action_sub.add_parser("drop"); ad.add_argument("id", type=int); ad.add_argument("--reason", required=True)
    ar = action_sub.add_parser("reopen"); ar.add_argument("id", type=int)
    for name in ("someday", "park"):
        parked = action_sub.add_parser(name); parked.add_argument("id", type=int)
    active = action_sub.add_parser("reactivate"); active.add_argument("id", type=int)
    defer = action_sub.add_parser("defer"); defer.add_argument("id", type=int); defer.add_argument("--until", required=True, type=require_iso_date)
    undelay_parser = action_sub.add_parser("undelay"); undelay_parser.add_argument("id", type=int)
    edit = action_sub.add_parser("edit"); edit.add_argument("id", type=int); edit.add_argument("--text", required=True); edit.add_argument("--reason", required=True); edit.add_argument("--force", action="store_true")
    n = sub.add_parser("next"); n.add_argument("--project", type=int)
    b = sub.add_parser("blocked"); b.add_argument("--project", type=int)
    d = sub.add_parser("done"); d.add_argument("id", type=int)
    s = sub.add_parser("show"); s.add_argument("kind", choices=("action", "project", "inbox")); s.add_argument("id", type=int)
    h = sub.add_parser("history"); h.add_argument("--limit", type=int, default=20)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            conn().close(); emit({"database": str(config.DB_PATH), "history": str(config.HISTORY_PATH)}, args.json); return 0
        if args.command == "history":
            rows = [] if not config.HISTORY_PATH.exists() else [json.loads(line) for line in config.HISTORY_PATH.read_text().splitlines()]
            emit(rows[-args.limit:], args.json); return 0
        connection = conn()
        if args.command == "capture": value = {"inbox_item": mutations.capture(connection, args.text, args.provenance)}
        elif args.command == "inbox": value = queries.inbox(connection, args.state)
        elif args.command == "clarify":
            fields = {"text": args.text, "project_id": args.project} if args.kind == "action" else {"outcome": args.text}
            if not args.text and args.kind != "nothing": raise mutations.GtdError("--text is required for action or project")
            value = {"id": mutations.clarify(connection, args.id, args.kind, reason=args.reason, **fields), "kind": args.kind}
        elif args.command == "project":
            if args.project_command == "add": value = {"project": mutations.add_project(connection, args.outcome, done_test=args.done_test)}
            elif args.project_command == "list": value = queries.projects(connection, args.state)
            elif args.project_command == "actions": value = project_actions(connection, args.id)
            else: value = close_project(connection, args.id, args.state)
        elif args.command == "action":
            if args.action_command == "add" and args.after is not None:
                prerequisite = connection.execute("SELECT state FROM action WHERE id=?", (args.after,)).fetchone()
                if prerequisite is None or prerequisite["state"] not in ("open", "done", "someday"):
                    raise mutations.GtdError("--after must name an existing open, someday, or completed action")
            if args.action_command == "add":
                blocks = [{"kind": "done", "ref_kind": "action", "ref_id": args.after}] if args.after is not None else None
                value = {"action": mutations.add_action(connection, args.text, project_id=args.project, commitment_type=args.type, blocks=blocks)}
            elif args.action_command == "drop": value = mutations.drop(connection, args.id, args.reason)
            elif args.action_command == "reopen": value = mutations.reopen(connection, args.id)
            elif args.action_command in ("someday", "park"):
                require_record(connection, "action", args.id)
                value = mutations.park(connection, args.id)
            elif args.action_command == "reactivate":
                require_record(connection, "action", args.id)
                value = mutations.reactivate(connection, args.id)
            elif args.action_command == "defer": value = {"block": defer(connection, args.id, args.until)}
            elif args.action_command == "undelay": value = {"action": args.id, **undelay(connection, args.id)}
            else:
                if args.force and not args.reason.strip(): raise mutations.GtdError("--force requires a non-empty --reason")
                value = {"correction": mutations.correct(connection, "action", args.id, "text", args.text, args.reason, force=args.force)}
        elif args.command == "next": value = queries.next_actions(connection, project_id=args.project)
        elif args.command == "blocked": value = queries.blocked_actions(connection, project_id=args.project)
        elif args.command == "done": value = mutations.done(connection, args.id)
        else: value = queries.show(connection, args.kind, args.id)
        connection.close(); emit(value, args.json); return 0
    except (mutations.GtdError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr); return 2


if __name__ == "__main__": raise SystemExit(main())
