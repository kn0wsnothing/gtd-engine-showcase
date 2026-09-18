"""The query surface.

Next-ness lives here and nowhere else. Nothing writes a `next` status, so the label cannot
degrade into a synonym for `open`; the stalled-project alarm is the same query returning
empty rather than something a review notices by eye.

Every function returns plain dicts. Formatting belongs to the CLI, so an agent reading this
surface gets stable structure rather than prose it has to parse back.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

from . import config, predicates
from .db import business_days_between, iso_week, today
from .predicates import UNBLOCKED

ACTION_COLS = """
    a.id, a.text, a.project_id, a.area_id, a.commitment_type, a.work_type,
    a.execution_type, a.person_id, a.hard_date, a.plan_date, a.commit_week,
    a.state, a.created_at, a.source_path, a.legacy_id
"""


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def _decorate(conn: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    """Attach the project outcome and the block list — the two things every surface needs
    and no caller should re-query."""
    for r in rows:
        if r.get("project_id"):
            p = conn.execute(
                "SELECT outcome, state FROM project WHERE id=?", (r["project_id"],)
            ).fetchone()
            r["project"] = p["outcome"] if p else None
        else:
            r["project"] = None
        work = predicates.is_work_record(conn, r.get("area_id"), r.get("project_id"))
        blocks = predicates.blocks_for(conn, r["id"])
        r["blocks"] = [
            {
                "id": b["id"],
                "kind": b["kind"],
                "satisfied": bool(b["satisfied"]),
                "description": predicates.describe(conn, b),
                "age_days": predicates.block_age_days(b, work=work),
            }
            for b in blocks
        ]
        r["blocked"] = any(not b["satisfied"] for b in r["blocks"])
        r["surfaced_without_moving"] = stale_count(conn, "action", r["id"], work=work)
    return rows


def stale_count(conn: sqlite3.Connection, kind: str, record_id: int, work: bool = False) -> int:
    """Surfacings with no state change since. Staleness is measured in surfacing cycles,
    never calendar time: an action never surfaced cannot be stale by neglect. A work-typed
    record's weekend cycles are not workable time and never count toward this; everything
    else counts every cycle, weekends included, since weekends are exactly when personal
    deep work happens."""
    if work:
        rows = conn.execute(
            "SELECT created_at FROM surfacing_event WHERE record_kind=? AND record_id=? AND moved=0",
            (kind, record_id),
        ).fetchall()
        n = 0
        for r in rows:
            try:
                d = date.fromisoformat((r["created_at"] or "")[:10])
            except ValueError:
                continue
            if d.weekday() < 5:
                n += 1
        return n
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM surfacing_event WHERE record_kind=? AND record_id=? AND moved=0",
        (kind, record_id),
    ).fetchone()
    return row["n"] or 0


# --- next-ness ------------------------------------------------------------------------

def next_actions(conn: sqlite3.Connection, project_id: int | None = None,
                 commitment_type: str | None = None, work_type: str | None = None,
                 execution_type: str | None = None, person_id: int | None = None,
                 area_id: int | None = None, exclude_gated: bool = False) -> list[dict]:
    """Actions that are open, live, and unblocked.

    Intentions are included: they are eligible work. What they are not is *selectable into
    Focus by mechanism* — that rule belongs to the band-two composer, not to next-ness.

    `exclude_gated` drops what is currently at the gate, so the operator's work list does not also
    carry the things he is about to be walked through. It is opt-in rather than the default
    on purpose: `stalled_projects` asks this same query whether a project has any next action
    at all, and a project whose only live action is waiting at the gate is not stalled. The
    two callers that read as "the operator's ripe work" — the top-level `gtd next` and the morning
    surface — pass it; project and area next-ness does not.

    Unfiltered, this is the operator's own next-ness, not the store's: an action whose `person_id`
    names someone else is that person's, not his, and is excluded by default. Asking for a
    person explicitly still works — that is what `person_id` as a filter is for — but silence
    on the parameter never means "everyone's."
    """
    sql = f"""
        SELECT {ACTION_COLS} FROM action a
        LEFT JOIN project p ON p.id = a.project_id
        WHERE a.state='open'
          AND (a.project_id IS NULL OR p.state='active')
          AND {UNBLOCKED}
    """
    params: list = []
    if project_id is not None:
        sql += " AND a.project_id=?"; params.append(project_id)
    if area_id is not None:
        sql += " AND a.area_id=?"; params.append(area_id)
    if commitment_type:
        sql += " AND a.commitment_type=?"; params.append(commitment_type)
    if work_type:
        sql += " AND a.work_type=?"; params.append(work_type)
    if execution_type:
        sql += " AND a.execution_type=?"; params.append(execution_type)
    if person_id is not None:
        sql += " AND a.person_id=?"; params.append(person_id)
    else:
        sql += " AND a.person_id IS NULL"
    sql += " ORDER BY a.hard_date IS NULL, a.hard_date, a.created_at"
    rows = _rows(conn.execute(sql, params))
    if exclude_gated:
        gated = gated_ids(conn)
        rows = [r for r in rows if r["id"] not in gated]
    return _decorate(conn, rows)


# --- the gate -------------------------------------------------------------------------

# An action is at the gate when it is open, it has at least one block, and every one of them
# is satisfied. The reason it could not be acted on is gone and it has not been acted on.
# Derived exactly the way next-ness is derived — nothing writes a `gate` status, so the gate
# cannot drift into a list someone curates.
HAS_BLOCK = "EXISTS (SELECT 1 FROM block b WHERE b.action_id = a.id)"


def agent_handback(conn: sqlite3.Connection, action_id: int) -> dict | None:
    """The satisfied, delivered agent handoff that put this action at the gate, if one did.

    Reads `waiting_for` through the block's foreign key rather than through `waiting()`, and
    that is the whole subtlety: a handback's waiting-for is by definition *closed*, and
    `waiting()` returns only open rows. Resolving the handoff through that view instead finds
    nothing, and the gate comes back empty and plausible. Join the table.

    A block is satisfied the moment its waiting-for closes, whatever state it closed in —
    `close_waiting` propagates on `dropped` exactly as it does on `delivered`. So a dropped
    handoff clears its block just as cleanly as a returned one, and picking the single latest
    row without checking state would read a dropped handoff as work an agent returned. Every
    agent handoff blocking this action has to be delivered for it to count as a handback at
    all: one dropped among them means nothing came back, even if another already delivered.
    """
    rows = conn.execute(
        "SELECT w.id, w.counterparty, w.expectation, w.lane, w.brief, w.state, w.reason, "
        "       w.dispatched_at, w.dispatch_path, w.closed_at, "
        "       b.id AS block_id, b.satisfied_at "
        "  FROM block b JOIN waiting_for w ON w.id = b.ref_id "
        " WHERE b.action_id=? AND b.kind='done' AND b.ref_kind='waiting_for' "
        "   AND b.satisfied=1 AND w.is_agent=1 "
        " ORDER BY b.satisfied_at DESC, b.id DESC", (action_id,)).fetchall()
    if not rows or any(r["state"] != "delivered" for r in rows):
        return None
    return dict(rows[0])


def gated_ids(conn: sqlite3.Connection) -> set[int]:
    """Ids currently at the gate, for the surfaces that must not also list them."""
    return {r["id"] for r in conn.execute(
        f"SELECT a.id FROM action a LEFT JOIN project p ON p.id = a.project_id "
        f" WHERE a.state='open' AND (a.project_id IS NULL OR p.state='active') "
        f"   AND {UNBLOCKED} AND {HAS_BLOCK}")}


def gate(conn: sqlite3.Connection) -> dict:
    """What came back for the operator, in two bands that are honestly different sizes of thing.

    `came_back` is work an agent was handed and has returned: short by construction, because
    the action is to look at what it did. `newly_actionable` is everything else whose blocks
    cleared — a date reached, a condition met, a person delivering — which is not promised to
    be short and is meant to be skimmed rather than cleared.
    """
    predicates.refresh_mechanical(conn)
    rows = _decorate(conn, _rows(conn.execute(f"""
        SELECT {ACTION_COLS} FROM action a
        LEFT JOIN project p ON p.id = a.project_id
        WHERE a.state='open'
          AND (a.project_id IS NULL OR p.state='active')
          AND {UNBLOCKED}
          AND {HAS_BLOCK}
    """)))
    came_back, newly = [], []
    for r in rows:
        handoff = agent_handback(conn, r["id"])
        if handoff:
            r["handoff"] = handoff
            came_back.append(r)
        else:
            newly.append(r)
    # Oldest handback first: a review that has waited three days should not sit under one that
    # landed this afternoon.
    came_back.sort(key=lambda r: (r["handoff"].get("satisfied_at") or "", r["id"]))
    newly.sort(key=lambda r: (r["hard_date"] is None, r["hard_date"] or "", r["created_at"]))
    return {"came_back": came_back, "newly_actionable": newly}


def blocked_actions(conn: sqlite3.Connection, project_id: int | None = None) -> list[dict]:
    """The suppressed set: open work held back, each with its blocking condition and age.
    This is the answer to 'what are you not showing me' at the mechanical level."""
    sql = f"""
        SELECT {ACTION_COLS} FROM action a
        LEFT JOIN project p ON p.id = a.project_id
        WHERE a.state='open' AND (a.project_id IS NULL OR p.state='active')
          AND NOT ({UNBLOCKED})
    """
    params: list = []
    if project_id is not None:
        sql += " AND a.project_id=?"; params.append(project_id)
    sql += " ORDER BY a.created_at"
    return _decorate(conn, _rows(conn.execute(sql, params)))


def project_open_actions(conn: sqlite3.Connection, project_id: int) -> tuple[list[dict], list[dict]]:
    """Every action still open under a project, regardless of the project's own state.

    `next_actions`/`blocked_actions` both gate on the *project* being active — right for a
    live project, wrong for the one case that needs this: the terminal render's basis for
    "what was outstanding when it closed" is asked exactly when the project itself is the
    thing that just went inactive. Returns (nexts, blocked), split the same way next-ness
    already splits it.
    """
    sql = f"""
        SELECT {ACTION_COLS} FROM action a
        WHERE a.state='open' AND a.project_id=?
        ORDER BY a.hard_date IS NULL, a.hard_date, a.created_at
    """
    rows = _decorate(conn, _rows(conn.execute(sql, (project_id,))))
    return [r for r in rows if not r["blocked"]], [r for r in rows if r["blocked"]]


def stalled_projects(conn: sqlite3.Connection) -> list[dict]:
    """Projects with no computable next action. Not an eyeball job — the alarm is the
    next-ness query returning empty."""
    out = []
    for p in conn.execute("SELECT * FROM project WHERE state='active' ORDER BY created_at"):
        if not next_actions(conn, project_id=p["id"]):
            row = dict(p)
            row["blocked_count"] = len(blocked_actions(conn, project_id=p["id"]))
            out.append(row)
    return out


def stuck_dispatches(conn: sqlite3.Connection) -> list[dict]:
    """A handoff stamped dispatched with neither a landing nor a refusal to show for it, past
    the point any real run — the queue wait, the job itself, a code lane's landing suite —
    could still be in flight.

    Deliberately blind to *why*. Whether the run never finished, finished and was refused for
    a reason `dispatch.reap_retryable` cannot yet tell apart from a real result, or the
    checkout never came up at all before anything could be written to the landing log — all of
    those read the same from here: `dispatched_at` stamped, `state` still open, too long ago.
    That is exactly the fact the automatic re-arm and its cap cannot promise to catch between
    them, and it is the fact that went unseen for 23 days on the advance-recurring job: it sat
    red on the fleet dashboard the whole time, and nobody connected the red to a dispatch that
    had quietly stopped being eligible for the next one. Being red and being un-requeueable are
    different facts, and this is the one that had nowhere to surface.
    """
    cutoff = (datetime.now() - timedelta(hours=config.DISPATCH_STUCK_HOURS)
             ).isoformat(timespec="seconds")
    rows = _rows(conn.execute(
        "SELECT * FROM waiting_for WHERE state='open' AND is_agent=1 "
        "  AND dispatched_at IS NOT NULL AND dispatched_at <= ? ORDER BY dispatched_at",
        (cutoff,)))
    for r in rows:
        dispatched = datetime.fromisoformat(r["dispatched_at"])
        r["dispatched_hours_ago"] = round((datetime.now() - dispatched).total_seconds() / 3600, 1)
        r["auto_retries"] = conn.execute(
            "SELECT COUNT(*) AS n FROM dispatch_retry WHERE waiting_for_id=? AND kind='auto'",
            (r["id"],)).fetchone()["n"]
    return rows


# --- the morning surface dataset ------------------------------------------------------

def hard_landscape(conn: sqlite3.Connection, day: str | None = None) -> list[dict]:
    """Band one. Date-and-time-specific commitments landing today, plus today's meetings.
    No selection at all, complete by definition, ordered only by the clock.

    Excludes an action whose `person_id` names someone other than the operator, the same rule
    `next_actions` applies: a hard date does not make someone else's deliverable his."""
    d = day or today()
    actions = _decorate(conn, _rows(conn.execute(
        f"SELECT {ACTION_COLS} FROM action a WHERE a.state='open' AND a.person_id IS NULL "
        "AND substr(a.hard_date,1,10)=? "
        "ORDER BY a.hard_date",
        (d,),
    )))
    return actions + meetings_on(conn, d)


def meetings_on(conn: sqlite3.Connection, day: str | None = None) -> list[dict]:
    from .entities import compose
    d = day or today()
    out = []
    for row in conn.execute("SELECT id FROM entity WHERE type='meeting' AND merged_into IS NULL"):
        instance_keys = [r["occurrence_key"] for r in conn.execute(
            "SELECT DISTINCT occurrence_key FROM entity_attribute "
            "WHERE entity_id=? AND occurrence_key<>'' ORDER BY occurrence_key",
            (row["id"],),
        )]
        for occurrence_key in [""] + instance_keys:
            f = compose(conn, row["id"], occurrence_key)
            start = f.get("start") or ""
            # An override whose RECURRENCE-ID equals the master's DTSTART replaces that
            # occurrence. Do not also surface the series-level copy (notably when the first
            # occurrence itself was declined or cancelled).
            if not occurrence_key and start in instance_keys:
                continue
            if not start.startswith(d):
                continue
            if (f.get("cancelled") or "").lower() in ("1", "true", "yes"):
                continue
            if (f.get("participation_status") or "").lower() == "declined":
                continue
            out.append({
                "kind": "meeting", "entity_id": row["id"],
                "occurrence_key": occurrence_key,
                "text": f.get("title") or "(untitled)", "hard_date": start,
                "classification": f.get("classification"),
                "prep_needed": f.get("prep_needed"),
            })
    return sorted(out, key=lambda r: r["hard_date"])


def week_commitments(conn: sqlite3.Connection, week: str | None = None) -> list[dict]:
    """Open and live commitments for the current ISO week. They carry into Focus in whole:
    the morning surface has no standing to second-guess a deliberate decision made three
    days earlier.

    That "in whole" is about which of the operator's commitments make the cut, never about whose
    commitment it is: an action whose `person_id` names someone other than the operator is excluded
    the same as everywhere else next-ness is computed, including when a correction reattributes
    it after it was already committed to the week."""
    w = week or iso_week()
    return _decorate(conn, _rows(conn.execute(
        f"SELECT {ACTION_COLS} FROM action a WHERE a.state='open' AND a.person_id IS NULL "
        "AND a.commit_week=? "
        "ORDER BY a.created_at",
        (w,),
    )))


def start_dates_reached(conn: sqlite3.Connection) -> list[dict]:
    """Actions whose stated start date arrived — the lookahead's engine.

    Lookahead runs on stated start dates rather than a flat distance from the due date,
    because a flat calendar window turns into noise inside a week. The date is stated once
    as a fact and corrected once if wrong; it is never re-derived from a guess about how
    long the work takes.
    """
    td = today()
    return _decorate(conn, _rows(conn.execute(
        f"""SELECT {ACTION_COLS} FROM action a
            JOIN block b ON b.action_id=a.id AND b.kind='date_reached'
            WHERE a.state='open' AND b.ref_date <= ? AND b.satisfied=1
              AND b.satisfied_at >= date(?, '-1 day')
            ORDER BY b.ref_date""",
        (td, td),
    )))


def about_to_stall(conn: sqlite3.Connection) -> list[dict]:
    """Obligations surfaced repeatedly with no state change.

    Scoped to obligations on purpose. An intention that has not moved is not stale, and
    firing this on one is the exact failure the obligation/intention split exists to prevent.
    """
    rows = next_actions(conn, commitment_type="obligation")
    return [r for r in rows if r["surfaced_without_moving"] >= config.STALE_SURFACING_CYCLES - 1]


def morning_surface(conn: sqlite3.Connection) -> dict:
    """Everything the morning run needs, in one call, so a run makes one query rather than
    reconstructing a view from six."""
    predicates.refresh_mechanical(conn)
    ripe = next_actions(conn, exclude_gated=True)
    commitments = week_commitments(conn)
    commit_ids = {r["id"] for r in commitments}
    return {
        "date": today(),
        "week": iso_week(),
        "band_one": hard_landscape(conn),
        "week_commitments": commitments,
        "ripe": [r for r in ripe if r["id"] not in commit_ids],
        "gate": gate(conn),
        "start_dates_reached": start_dates_reached(conn),
        "about_to_stall": about_to_stall(conn),
        "waiting_ripe": [w for w in waiting(conn) if w["ripe"]],
        "clarify_ready": inbox(conn, state="clarify_ready"),
        "stalled_projects": stalled_projects(conn),
        "stuck_dispatches": stuck_dispatches(conn),
    }


# --- waiting-for ----------------------------------------------------------------------

def waiting(conn: sqlite3.Connection) -> list[dict]:
    """The waiting-for list with decay clocks.

    `expected_by` is explicitly not a hard commitment — it only calibrates when the item
    goes ripe. When the expectation passes the item does not chase; it surfaces at the next
    rhythm point with a drafted follow-up.
    """
    out = []
    for r in conn.execute("SELECT * FROM waiting_for WHERE state='open' ORDER BY created_at"):
        row = dict(r)
        anchor = r["expected_by"] or _plus_days(r["created_at"], config.WAITING_DECAY_DAYS)
        row["ripe_on"] = anchor
        row["ripe"] = bool(anchor) and anchor <= today()
        row["age_days"] = _age_days(r["created_at"])
        # The ladder terminates rather than loops: first ripe is a drafted follow-up, second
        # is a question about the dependency, third is a proposed drop defaulted to dropping.
        row["ladder"] = min(int(r["ripe_cycles"]) + 1, 3) if row["ripe"] else 0
        if row["ladder"] == 3:
            row["affects"] = [
                b["action_id"] for b in conn.execute(
                    "SELECT action_id FROM block WHERE kind='done' AND ref_kind='waiting_for' AND ref_id=?",
                    (r["id"],),
                )
            ]
        out.append(row)
    return out


def _plus_days(ts: str | None, days: int) -> str | None:
    if not ts:
        return None
    try:
        return (date.fromisoformat(ts[:10]) + timedelta(days=days)).isoformat()
    except ValueError:
        return None


def _age_days(ts: str | None, work: bool = False) -> int:
    if not ts:
        return 0
    try:
        created = date.fromisoformat(ts[:10])
    except ValueError:
        return 0
    if work:
        return business_days_between(created, date.today())
    return (date.today() - created).days


# --- agendas --------------------------------------------------------------------------

def agenda(conn: sqlite3.Connection, person_id: int) -> list[dict]:
    """What is live and raise-able with one person: actions blocked on conversational
    proximity to them. Deliberately thin and short-lived — the rich note is the substance,
    and it is the agenda surface that stays pruned, not the note."""
    return _decorate(conn, _rows(conn.execute(
        f"""SELECT DISTINCT {ACTION_COLS} FROM action a
            JOIN block b ON b.action_id=a.id
            WHERE a.state='open' AND b.kind='proximity' AND b.ref_entity_id=?
            ORDER BY a.created_at""",
        (person_id,),
    )))


# --- inbox, projects, areas -----------------------------------------------------------

def inbox(conn: sqlite3.Connection, state: str | None = None) -> list[dict]:
    sql = "SELECT * FROM inbox_item"
    params: list = []
    if state:
        sql += " WHERE state=?"; params.append(state)
    else:
        sql += " WHERE state IN ('captured','clarify_ready')"
    sql += " ORDER BY captured_at"
    return _rows(conn.execute(sql, params))


def projects(conn: sqlite3.Connection, state: str = "active") -> list[dict]:
    rows = _rows(conn.execute(
        "SELECT * FROM project WHERE state=? ORDER BY created_at", (state,)
    ))
    for r in rows:
        r["next_count"] = len(next_actions(conn, project_id=r["id"]))
        r["blocked_count"] = len(blocked_actions(conn, project_id=r["id"]))
    return rows


def areas(conn: sqlite3.Connection) -> list[dict]:
    rows = _rows(conn.execute("SELECT * FROM area WHERE state='active' ORDER BY name"))
    for r in rows:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM action WHERE area_id=? AND created_at >= date('now','-30 day')",
            (r["id"],),
        ).fetchone()
        # An area producing no actions for a month is either dormant or being neglected,
        # and those need different responses — so the count is surfaced, not judged.
        r["actions_last_30d"] = row["n"] or 0
    return rows


def show(conn: sqlite3.Connection, kind: str, record_id: int) -> dict:
    table = {"action": "action", "project": "project", "area": "area",
             "waiting": "waiting_for", "inbox": "inbox_item", "entity": "entity"}[kind]
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
    if row is None:
        raise KeyError(f"{kind} #{record_id} not found")
    out = dict(row)
    out["_kind"] = kind
    if kind == "action":
        out = _decorate(conn, [out])[0]
    if kind == "entity":
        from .entities import compose
        out["composed"] = compose(conn, record_id)
        out["layers"] = _rows(conn.execute(
            "SELECT field, value, provenance, occurrence_key, updated_at FROM entity_attribute "
            "WHERE entity_id=? ORDER BY field, provenance", (record_id,)))
    store_kind = {"waiting": "waiting_for", "inbox": "inbox_item"}.get(kind, kind)
    out["corrections"] = _rows(conn.execute(
        "SELECT * FROM correction WHERE record_kind=? AND record_id=? ORDER BY created_at",
        (store_kind, record_id)))
    out["surfacings"] = _rows(conn.execute(
        "SELECT cycle, band, moved, created_at FROM surfacing_event "
        "WHERE record_kind=? AND record_id=? ORDER BY created_at",
        (store_kind, record_id)))
    if kind == "project":
        # A project can be an owner in its own right, or the filing target of a rule an area
        # owns — `project_id` is the "files into" override `generate.add_rule` documents.
        # Found live 2026-08-03: a project's only cadence rule was invisible from here, so the
        # weekly review that needed to know "does this recurrence still exist" had no way to
        # ask the store directly and a since-superseded manual stand-in was created instead.
        out["rules"] = _rows(conn.execute(
            "SELECT id, owner_kind, owner_id, kind, expr, action_text, anchor_date, "
            "       last_generated, state FROM generation_rule "
            "WHERE state='active' AND ((owner_kind='project' AND owner_id=?) OR project_id=?) "
            "ORDER BY id", (record_id, record_id)))
    elif kind == "area":
        out["rules"] = _rows(conn.execute(
            "SELECT id, owner_kind, owner_id, kind, expr, action_text, anchor_date, "
            "       last_generated, state FROM generation_rule "
            "WHERE state='active' AND owner_kind='area' AND owner_id=? ORDER BY id",
            (record_id,)))
    return out


# --- interrogation --------------------------------------------------------------------

def handoff_for(conn: sqlite3.Connection, action_id: int) -> dict | None:
    """The agent waiting-for that suppresses this action, if one exists.

    Read by the surface that has just created a pair and needs to say so. A clarification that
    reports only the action id makes the handoff take a second command to see, and the handoff
    is the half that has never worked.
    """
    row = conn.execute(
        "SELECT w.id, w.counterparty, w.expectation, w.lane, w.state, w.dispatched_at, "
        "       b.id AS block_id, b.satisfied "
        "  FROM block b JOIN waiting_for w ON w.id = b.ref_id "
        " WHERE b.action_id=? AND b.kind='done' AND b.ref_kind='waiting_for' "
        " ORDER BY b.id DESC LIMIT 1", (action_id,)).fetchone()
    return dict(row) if row else None


_CLOSED_RECORD_STATES = {
    "action": ("action", {"done", "dropped"}),
    "project": ("project", {"done", "dropped", "demoted"}),
    "waiting": ("waiting_for", {"delivered", "dropped", "converted"}),
    "inbox": ("inbox_item", {"clarified", "discarded", "reference"}),
    "area": ("area", {"archived"}),
}


def _decision_subject_closed(conn: sqlite3.Connection, record_kind: str, record_id: int) -> bool:
    """True once the record a withheld-decision event points at has since closed.

    The decision log is an append-only audit trail — it is never rewritten when the record
    it was about later closes — so "is this still live" has to be answered at read time
    against current state, not baked into the row when it was written. A record gone
    entirely (deleted, or a kind this map does not cover) is not something to raise either:
    there is nothing left to attach a disposition to. `action:558` ("Book the return
    shinkansen"), dropped 2026-08-20, still showing up in the 2026-08-22 weekly packet's
    withheld set is the bug this guards against.
    """
    entry = _CLOSED_RECORD_STATES.get(record_kind)
    if entry is None:
        return False
    table, closed_states = entry
    row = conn.execute(
        f"SELECT state FROM {table} WHERE id=?", (record_id,)  # noqa: S608 - table from a closed map
    ).fetchone()
    return row is None or row["state"] in closed_states


def withheld_on(conn: sqlite3.Connection, record_kind: str, day: str) -> set[int]:
    """Ids the run logged a defended reason for not showing, in any cycle on `day`.

    Matched on the day rather than an exact cycle string because the morning run, an
    interactive review and a re-render all stamp different cycles for the same date, and a
    suppression the operator justified at the review has to survive the next render of that day.
    """
    rows = conn.execute(
        "SELECT DISTINCT record_id FROM withheld_decision WHERE record_kind=? AND cycle LIKE ?",
        (record_kind, f"%{day}%"),
    ).fetchall()
    return {r["record_id"] for r in rows}


def withheld(conn: sqlite3.Connection, cycle: str | None = None, *, since: str | None = None,
             limit: int = 200, offset: int = 0) -> dict:
    """'What are you not showing me, and why.'

    Two sources, deliberately kept apart. The blocked set is mechanical suppression — every
    item with its condition and its age. The withheld-decisions log is the agent's own
    selection calls, which is the half that would otherwise be unauditable. You audit the
    judgment rather than the inventory.

    `since` and the window are optional and default to the historical behaviour, because the
    interactive command's job is still "show me everything you held back". A remote surface
    asks the same question of a log that only grows, so it passes bounds; the counts come back
    beside the page so a bounded answer never reads as a complete one.

    A decision event whose subject record has since closed — done, dropped, demoted, or the
    kind's own equivalent, per `_decision_subject_closed` — is dropped before counting or
    paging. Re-litigating a call about a record that is already dead is not auditing the
    judgment, it is just noise (2026-08-24, found via `action:558` resurfacing a week after
    the operator dropped it).
    """
    sql = "SELECT * FROM withheld_decision"
    where: list[str] = []
    params: list = []
    if cycle:
        where.append("cycle=?"); params.append(cycle)
    if since:
        where.append("created_at >= ?"); params.append(since)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC"
    all_decisions = _rows(conn.execute(sql, params))

    live: list[dict] = []
    excluded_closed = 0
    for d in all_decisions:
        if _decision_subject_closed(conn, d["record_kind"], d["record_id"]):
            excluded_closed += 1
        else:
            live.append(d)

    decisions = live[offset: offset + limit]
    for d in decisions:
        if d["record_kind"] == "action":
            row = conn.execute("SELECT text FROM action WHERE id=?", (d["record_id"],)).fetchone()
            d["text"] = row["text"] if row else None
    return {
        "blocked": blocked_actions(conn),
        "decisions": decisions,
        "decisions_total": len(live),
        "decisions_excluded_closed": excluded_closed,
        "someday": _rows(conn.execute(
            "SELECT id, text, commitment_type, created_at FROM action WHERE state='someday' "
            "ORDER BY created_at")),
    }


def bench(conn: sqlite3.Connection) -> list[dict]:
    """Band three: everything else live and eligible, held rather than displayed.

    Never pushed — a visible list of uncommitted eligible work is exactly the open-loop
    problem the suppression design exists to kill — and returned the instant the operator asks.
    """
    commit_ids = {r["id"] for r in week_commitments(conn)}
    return [r for r in next_actions(conn) if r["id"] not in commit_ids]


def drift_signals(conn: sqlite3.Connection) -> list[dict]:
    """The same `record_kind:field` *shape* of correction recurring three or more times is a
    field-level signal, not a semantic one — it names where corrections cluster, not why. A
    2026-W34 review of the `action:text` shape found a date moving, a completed step dropped,
    a build split from a presentation, a success measure added, a stale dependency reversed,
    and one genuine scope-invention failure, all sharing the one shape. The aggregate alone
    cannot support a claim that a spec is wrong; only the underlying corrections can.

    So each returned shape carries its aggregate (`shape`, `n`, `latest`, unchanged for
    existing consumers) plus `evidence`: its `config.DRIFT_EVIDENCE_LIMIT` most recent live
    corrections, most-recent-first, each with the id, record, field, old/new value, reason and
    timestamp a weekly packet needs to classify the shape before naming a candidate fix — never
    to infer a semantic cause itself, which is a reader's job, not this query's.
    """
    shapes = _rows(conn.execute(
        "SELECT shape, COUNT(*) AS n, MAX(created_at) AS latest FROM correction "
        "WHERE retired=0 GROUP BY shape HAVING n >= 3 ORDER BY n DESC"))
    for s in shapes:
        s["evidence"] = _rows(conn.execute(
            "SELECT id, record_kind, record_id, field, old_value, new_value, reason, created_at "
            "FROM correction WHERE retired=0 AND shape=? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (s["shape"], config.DRIFT_EVIDENCE_LIMIT)))
    return shapes


def _open_unsatisfied_blocks(conn: sqlite3.Connection) -> list[dict]:
    """Every block still holding an open action back — the base set both weekly-review
    staleness questions are drawn from. `unexamined_blocks` asks how long one has sat;
    `stale_conditions` asks whether it still refers to something real. Same set, two
    different judgments, so it lives once."""
    return _rows(conn.execute(
        "SELECT b.*, a.text, a.area_id, a.project_id FROM block b "
        "JOIN action a ON a.id=b.action_id WHERE b.satisfied=0 AND a.state='open'"))


def stale_conditions(conn: sqlite3.Connection) -> list[dict]:
    """Blocks whose condition itself has decayed, per `predicates.stale_condition` —
    complementary to `unexamined_blocks`: age alone (`config.BLOCK_UNEXAMINED_DAYS`) only
    catches a block once it has sat a long time, whatever its kind. This catches a `done`
    block on an already-dropped action on day one just as well as day thirty, and a
    `proximity` block against a person with nothing on the calendar however young it is.

    Surfaced, never auto-cleared: a stale-looking condition is a question for the weekly
    review, not something this query has standing to resolve on its own.
    """
    out = []
    for b in _open_unsatisfied_blocks(conn):
        work = predicates.is_work_record(conn, b.get("area_id"), b.get("project_id"))
        reason = predicates.stale_condition(conn, b, work=work)
        if reason:
            b = dict(b)
            b["reason"] = reason
            b["description"] = predicates.describe(conn, b)
            b["age_days"] = predicates.block_age_days(b, work=work)
            out.append(b)
    return out


def weekly_packet(conn: sqlite3.Connection) -> dict:
    """Everything the weekly must carry, each element ready for a drafted disposition.

    Notably absent: intentions. The weekly does not raise them — not their age, not their
    non-movement, not a gentle version of either. That is the monthly's job.
    """
    predicates.refresh_mechanical(conn)
    unexamined = [
        b for b in _open_unsatisfied_blocks(conn)
        if _age_days(b["created_at"],
                     work=predicates.is_work_record(conn, b["area_id"], b["project_id"])
                     ) >= config.BLOCK_UNEXAMINED_DAYS
    ]
    return {
        "week": iso_week(),
        "suppressed_by_project": {
            p["outcome"]: blocked_actions(conn, project_id=p["id"])
            for p in conn.execute("SELECT id, outcome FROM project WHERE state='active'")
        },
        "stalled_projects": stalled_projects(conn),
        "unexamined_blocks": unexamined,
        "stale_conditions": stale_conditions(conn),
        "done_tests_to_reask": _rows(conn.execute(
            "SELECT id, outcome, done_test, done_test_checked_at FROM project "
            "WHERE state='active' AND (done_test_checked_at IS NULL "
            "OR done_test_checked_at < date('now','-28 day'))")),
        "areas": areas(conn),
        "withheld": withheld(conn),
        "inbox": inbox(conn),
        "waiting": [w for w in waiting(conn) if w["ripe"]],
        "rejections": _rows(conn.execute(
            "SELECT * FROM rejection WHERE created_at >= date('now','-7 day') ORDER BY created_at")),
        "drift_signals": drift_signals(conn),
    }


def monthly_intentions(conn: sqlite3.Connection) -> list[dict]:
    """The monthly's one job from this design: intentions that have not moved in three
    months, each for a status decision — never a prompt to do it.

    Three permitted outcomes, all the operator's: it becomes an obligation with a real date and
    something else gives; it goes to someday/maybe explicitly; or it stays an intention and
    the system stops raising it.
    """
    cutoff = (date.today() - timedelta(days=config.INTENTION_STATUS_CHECK_DAYS)).isoformat()
    rows = _rows(conn.execute(
        "SELECT * FROM action WHERE state='open' AND commitment_type='intention' AND created_at < ?",
        (cutoff,)))
    for r in rows:
        last = conn.execute(
            "SELECT MAX(created_at) AS m FROM correction WHERE record_kind='action' AND record_id=?",
            (r["id"],)).fetchone()
        r["last_touched"] = last["m"] or r["created_at"]
        r["age_days"] = _age_days(r["created_at"])
    return [r for r in rows if r["last_touched"] < cutoff]


# --- collision detection --------------------------------------------------------------------

_STOP = {"the", "a", "an", "and", "or", "to", "of", "for", "on", "in", "it", "is", "with",
         "that", "this", "at", "by", "from", "as", "be", "her", "his", "their", "them", "she",
         "he", "they", "we", "you", "i", "my", "our", "up", "out", "into", "over", "run",
         "running", "do", "doing"}


def _tokens(text: str) -> set[str]:
    import re
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 2 and w not in _STOP}


def _overlap(a: set, b: set) -> tuple[float, int]:
    """Containment, not Jaccard.

    Jaccard punishes length difference, and the real case is exactly that shape: a long capture
    quoting a meeting against a short existing record. "Complete the weekly data update handoff
    to Freya, aiming to pass close to 100% of it to her" versus "Freya: try running the weekly
    data update process independently on Monday" share four content words and score 0.29 by
    Jaccard — under any useful threshold — while being plainly the same thread. Containment over
    the shorter side scores it 0.5. A minimum count of shared words keeps short strings from
    matching on one accidental word.
    """
    shared = a & b
    if not shared:
        return 0.0, 0
    return len(shared) / max(1, min(len(a), len(b))), len(shared)


def similar_records(conn: sqlite3.Connection, text: str, project_id: int | None = None,
                    threshold: float = 0.45, min_shared: int = 3) -> list[dict]:
    """Live records that may already be the thing `text` describes.

    A capture very often describes work already in flight, and the clarify run cannot see that:
    it is told which projects exist, not what any of them already holds. Left to prose, a miss
    becomes an invisible duplicate — two records for one thread, disagreeing about when it
    happens. So the store answers it instead, and the answer rides along with the draft to the
    moment of commitment, where a miss is still visible.

    Overlap is Jaccard over content words. It is deliberately blunt: the output is a prompt for
    judgment, never a verdict, and a false positive costs one glance while a false negative
    costs a duplicated commitment.
    """
    want = _tokens(text)
    if not want:
        return []
    out: list[dict] = []

    sql = ("SELECT id, text, state, project_id, plan_date, hard_date FROM action "
           "WHERE state IN ('open', 'someday')")
    params: list = []
    if project_id is not None:
        sql += " AND project_id = ?"
        params.append(project_id)
    for r in conn.execute(sql, params):
        score, shared = _overlap(want, _tokens(r["text"]))
        if score >= threshold and shared >= min_shared:
            out.append({"kind": "action", "id": r["id"], "text": r["text"],
                        "state": r["state"], "project_id": r["project_id"],
                        "plan_date": r["plan_date"], "hard_date": r["hard_date"],
                        "overlap": round(score, 2)})

    wsql = ("SELECT id, counterparty, expectation, expected_by, project_id FROM waiting_for "
            "WHERE state = 'open'")
    wparams: list = []
    if project_id is not None:
        wsql += " AND project_id = ?"
        wparams.append(project_id)
    for r in conn.execute(wsql, wparams):
        blob = f"{r['counterparty']} {r['expectation']}"
        score, shared = _overlap(want, _tokens(blob))
        if score >= threshold and shared >= min_shared:
            out.append({"kind": "waiting_for", "id": r["id"], "text": blob,
                        "counterparty": r["counterparty"], "expected_by": r["expected_by"],
                        "project_id": r["project_id"], "overlap": round(score, 2)})

    return sorted(out, key=lambda r: -r["overlap"])
