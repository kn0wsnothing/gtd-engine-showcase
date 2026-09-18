"""Blocking predicate evaluation and transaction-time propagation.

The goal is suppression, not a graph. The problem is not knowing the correct order of ten
actions — it is keeping nine of them invisible until they are real. So there is no forward
adjacency list and no global DAG: a block is one backward pointer on the blocked action,
created at clarify, and a wrong one corrupts only its own action.

An action holds a flat AND-set of blocks. No nesting, no OR.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config
from .db import business_days_between, now, today

# An action is unblocked when it has no unsatisfied block. Used verbatim by every query
# that computes next-ness, so "next" cannot drift into a synonym for "open".
UNBLOCKED = "NOT EXISTS (SELECT 1 FROM block b WHERE b.action_id = a.id AND b.satisfied = 0)"


def _clip(text: str, limit: int = 60) -> str:
    """Clip to a readable length without emitting a half-open wikilink.

    These strings are projected into vault notes, and a `[[` the clip cut off from its `]]`
    is a broken link in Obsidian rather than a shortened one, so the cut backs off to before
    the opener.
    """
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    head = text[:limit].rsplit(" ", 1)[0]
    if head.count("[[") > head.count("]]"):
        head = head[:head.rindex("[[")].rstrip()
    return head + "…"


def label(conn: sqlite3.Connection, kind: str, ref_id: int | None) -> str:
    """What a referenced record is, in the words it was captured in.

    Every surface that shows a block shows this string, so an id here is an id in front of
    the operator — and "action #215" tells him nothing about whether it matters, costs him a lookup
    to find out, and reads as the store talking about itself. The id stays as a trailing
    handle because it is what he types back into the next command. The handle names its own
    table (e.g. "waiting-for 16") rather than a bare "(#16)" — ids are not unique across
    tables, so a bare number reads as whichever record the reader happens to have in mind.
    """
    if ref_id is None:
        return "something unrecorded"
    table, col = {
        "action": ("action", "text"),
        "project": ("project", "outcome"),
        "waiting_for": ("waiting_for", None),
        "waiting": ("waiting_for", None),
        "entity": ("entity", None),
    }.get(kind, (None, None))
    kind_label = "waiting-for" if kind in ("waiting_for", "waiting") else kind
    if table is None:
        return f"{kind} #{ref_id}"
    if table == "entity":
        from . import entities
        name = entities.compose(conn, ref_id).get("name")
        return f"{name} ({kind_label} {ref_id})" if name else f"entity #{ref_id}"
    if table == "waiting_for":
        row = conn.execute(
            "SELECT counterparty, expectation FROM waiting_for WHERE id=?", (ref_id,)).fetchone()
        if row is None:
            return f"a waiting-for that no longer exists (#{ref_id})"
        return f"{row['counterparty']}: {_clip(row['expectation'])} ({kind_label} {ref_id})"
    row = conn.execute(f"SELECT {col} FROM {table} WHERE id=?", (ref_id,)).fetchone()
    if row is None:
        return f"a {kind} that no longer exists (#{ref_id})"
    return f"{_clip(row[col])} ({kind_label} {ref_id})"


def describe(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Human-readable rendering of a block, for surfaces and the weekly's suppressed set."""
    k = row["kind"]
    if k == "done":
        return f"until {label(conn, row['ref_kind'], row['ref_id'])} is done"
    if k == "date_reached":
        return f"until {row['ref_date']}"
    if k == "exists":
        return f"until {row['ref_path']} exists"
    if k == "confirmed":
        return f"until {label(conn, 'entity', row['ref_entity_id'])} has confirmed"
    if k == "proximity":
        return (f"until a conversation with "
                f"{label(conn, 'entity', row['ref_entity_id'])} is imminent")
    return f"until {row['prose']}"


def _satisfy(conn: sqlite3.Connection, block_id: int, value: bool) -> None:
    ts = now()
    if value:
        conn.execute(
            "UPDATE block SET satisfied=1, satisfied_at=COALESCE(satisfied_at,?), "
            "last_evaluated=? WHERE id=?",
            (ts, ts, block_id),
        )
    else:
        conn.execute(
            "UPDATE block SET satisfied=0, satisfied_at=NULL, last_evaluated=? WHERE id=?",
            (ts, block_id),
        )


def refresh_mechanical(conn: sqlite3.Connection) -> dict[str, int]:
    """Re-evaluate the kinds that can be checked without judgment.

    `date_reached` is arithmetic. `exists` is a stat call at rhythm points. `proximity` is
    checked against the calendar mirror already materialised into the entity layer. All
    three are recomputed rather than latched, except where the operator flipped one by hand — a
    manual override is sticky, because "I'm about to call a person" is a fact the calendar does
    not know.

    `confirmed`, `judgment` and `done` are not touched here: the first two wait on evidence
    the agent observes, and the third is driven by transaction-time propagation.
    """
    counts = {"date_reached": 0, "exists": 0, "proximity": 0}
    td = today()

    for row in conn.execute(
        "SELECT * FROM block WHERE kind='date_reached' AND manual_override=0"
    ).fetchall():
        want = bool(row["ref_date"]) and row["ref_date"] <= td
        if bool(row["satisfied"]) != want:
            _satisfy(conn, row["id"], want)
            counts["date_reached"] += 1

    for row in conn.execute(
        "SELECT * FROM block WHERE kind='exists' AND manual_override=0"
    ).fetchall():
        p = row["ref_path"] or ""
        candidate = Path(p) if p.startswith("/") else config.VAULT / p
        want = bool(p) and candidate.exists()
        if bool(row["satisfied"]) != want:
            _satisfy(conn, row["id"], want)
            counts["exists"] += 1

    imminent = _imminent_person_ids(conn)
    for row in conn.execute(
        "SELECT * FROM block WHERE kind='proximity' AND manual_override=0"
    ).fetchall():
        want = row["ref_entity_id"] in imminent
        if bool(row["satisfied"]) != want:
            _satisfy(conn, row["id"], want)
            counts["proximity"] += 1

    return counts


def _imminent_person_ids(conn: sqlite3.Connection) -> set[int]:
    """People the operator has a meeting with inside the prep window.

    Read from the entity layer's composed `start` and `attendee_ids`, which calendar-sync
    materialises. Showing a a person item on a day he will not see a person is noise, and noise
    in this position teaches him to skim — so the window is deliberately short.
    """
    from .entities import compose

    horizon = datetime.now() + timedelta(hours=config.PROXIMITY_PREP_WINDOW_H)
    out: set[int] = set()
    for row in conn.execute("SELECT id FROM entity WHERE type='meeting' AND merged_into IS NULL"):
        fields = compose(conn, row["id"])
        start = fields.get("start")
        if not start:
            continue
        try:
            when = datetime.fromisoformat(start)
        except ValueError:
            continue
        if not (datetime.now() - timedelta(hours=2) <= when <= horizon):
            continue
        if (fields.get("cancelled") or "").lower() in ("1", "true", "yes"):
            continue
        for pid in (fields.get("attendee_ids") or "").split(","):
            pid = pid.strip()
            if pid.isdigit():
                out.add(int(pid))
    return out


def _has_future_meeting(conn: sqlite3.Connection, person_id: int) -> bool:
    """Whether the calendar mirror holds *any* still-ahead, non-cancelled meeting naming this
    person — however far out it reaches. Deliberately unbounded, unlike `_imminent_person_ids`'s
    short prep window: that window answers "raise it now?" and is expected to be empty most of
    the time, which is the whole point of a proximity block. This answers a different question
    — "could this condition ever come true?" — so it has to look at the whole mirror, not the
    next two days of it.
    """
    from .entities import compose

    now_dt = datetime.now()
    for row in conn.execute("SELECT id FROM entity WHERE type='meeting' AND merged_into IS NULL"):
        fields = compose(conn, row["id"])
        start = fields.get("start")
        if not start:
            continue
        try:
            when = datetime.fromisoformat(start)
        except ValueError:
            continue
        if when < now_dt:
            continue
        if (fields.get("cancelled") or "").lower() in ("1", "true", "yes"):
            continue
        attendees = {pid.strip() for pid in (fields.get("attendee_ids") or "").split(",")}
        if str(person_id) in attendees:
            return True
    return False


# States a `done`-block's referent can sit in while the condition is still reachable. Anything
# else — the row is gone, or its state is outside this set — means the referent has taken a
# path that will never fire `propagate_done`, so the block is waiting on something that cannot
# happen. `someday` and `on-hold` stay "alive": both are reversible, so the condition can still
# come true later, and flagging them would teach the reader to ignore a park that is working as
# designed.
_DONE_REF_ALIVE_STATES = {
    "action": {"open", "someday"},
    "project": {"active", "someday", "on-hold"},
    "waiting_for": {"open"},
}


def _done_ref_is_dead(conn: sqlite3.Connection, ref_kind: str, ref_id: int) -> str:
    """Empty string when the referent can still reach done; otherwise the reason it cannot."""
    table = {"action": "action", "project": "project", "waiting_for": "waiting_for"}.get(ref_kind)
    if table is None:
        return ""
    row = conn.execute(f"SELECT state FROM {table} WHERE id=?", (ref_id,)).fetchone()
    if row is None:
        return "no longer exists"
    alive = _DONE_REF_ALIVE_STATES.get(ref_kind, set())
    if row["state"] not in alive:
        return f"is {row['state']}, not done"
    return ""


def stale_condition(conn: sqlite3.Connection, row: sqlite3.Row, work: bool = False) -> str | None:
    """Whether a block's *condition* has decayed, as distinct from how long it has sat.

    `block_age_days` answers "how old is this" — a `date_reached` block two days from firing
    and a `proximity` block whose person was never on any calendar are the same age and
    nothing about age alone tells them apart. This answers a different question per kind,
    because "gone stale" means something different for each of the six:

    - `done` — the referent (action/project/waiting_for) took a path that can never trigger
      `propagate_done`: it was dropped, demoted, or the row is simply gone. Checked directly
      against the referent's own state; age is irrelevant; a block minted an hour ago against
      an already-dropped action is exactly as dead as one minted a year ago.
    - `date_reached` — the date is in the past and the block is still unsatisfied. Mechanical
      refresh clears this every cycle when `manual_override=0`, so a live instance here means
      either an override that has outlived the fact it was overriding, or a missed refresh —
      both worth a look regardless of cause.
    - `exists` — symmetric to `date_reached`: the path already exists but the block is still
      unsatisfied, meaning a refresh cycle was missed since it appeared.
    - `proximity` — no meeting naming this person exists anywhere ahead in the calendar
      mirror, not merely outside the short imminent-conversation window. "Not imminent yet"
      is the block working as designed; "never on the calendar at all" is the condition that
      cannot come true no matter how long it sits.
    - `confirmed`, `judgment` — neither has a mechanical fact to check against: confirmation
      waits on a reply this store never sees, and a judgment predicate is prose by
      definition (see `lint.check_block`). Age is the only signal available, so both fall
      back to it — `config.BLOCK_JUDGMENT_STALE_DAYS`, on the same business/calendar clock as
      every other staleness measure in this module.

    Returns the reason a condition looks dead, or `None` when it still looks live. Read-only:
    a hit here is a question for the weekly review (`gtd weekly-packet`'s `stale_conditions`),
    never grounds to clear or drop the block unattended.
    """
    if row["satisfied"]:
        return None
    kind = row["kind"]

    if kind == "done":
        if row["ref_kind"] is None or row["ref_id"] is None:
            return None  # unevaluable at all — the evaluability lint already names this
        why = _done_ref_is_dead(conn, row["ref_kind"], row["ref_id"])
        if not why:
            return None
        if why == "no longer exists":
            return f"waits on {row['ref_kind']} #{row['ref_id']}, which no longer exists"
        return f"waits on {label(conn, row['ref_kind'], row['ref_id'])}, which {why}"

    if kind == "date_reached":
        ref_date = row["ref_date"]
        if not ref_date:
            return None
        try:
            when = date.fromisoformat(ref_date[:10])
        except ValueError:
            return None
        if when >= date.today():
            return None
        days = (date.today() - when).days
        return f"{ref_date} passed {days} day{'s' if days != 1 else ''} ago without clearing"

    if kind == "exists":
        p = row["ref_path"] or ""
        if not p:
            return None
        candidate = Path(p) if p.startswith("/") else config.VAULT / p
        if candidate.exists():
            return f"{p} already exists, but the block has not cleared"
        return None

    if kind == "proximity":
        if row["ref_entity_id"] is None:
            return None
        if _has_future_meeting(conn, row["ref_entity_id"]):
            return None
        return f"no upcoming calendar event with {label(conn, 'entity', row['ref_entity_id'])}"

    if kind in ("confirmed", "judgment"):
        age = block_age_days(row, work=work)
        if age < config.BLOCK_JUDGMENT_STALE_DAYS:
            return None
        unit = "business day" if work else "day"
        return (f"sitting {age} {unit}{'s' if age != 1 else ''} with nothing that could ever "
                f"mechanically confirm or deny it — a {kind}() block only ever moves when a "
                "person or agent says so")

    return None


def propagate_done(conn: sqlite3.Connection, ref_kind: str, ref_id: int) -> list[int]:
    """A record reached done — what was waiting on it?

    A cheap reverse lookup rather than a maintained edge, run inside the same transaction as
    the write that triggered it. Returns the action ids that became fully unblocked, which
    is what lets a completion propose "this just unblocked X" in the same breath.
    """
    ts = now()
    conn.execute(
        "UPDATE block SET satisfied=1, satisfied_at=?, last_evaluated=? "
        "WHERE kind='done' AND ref_kind=? AND ref_id=? AND satisfied=0",
        (ts, ts, ref_kind, ref_id),
    )
    rows = conn.execute(
        """
        SELECT DISTINCT a.id FROM action a
        JOIN block b ON b.action_id = a.id
        WHERE b.kind='done' AND b.ref_kind=? AND b.ref_id=? AND a.state='open'
          AND NOT EXISTS (SELECT 1 FROM block b2 WHERE b2.action_id=a.id AND b2.satisfied=0)
        """,
        (ref_kind, ref_id),
    ).fetchall()
    return [r["id"] for r in rows]


def propagate_reopen(conn: sqlite3.Connection, ref_kind: str, ref_id: int) -> list[int]:
    """The inverse: a record left done, so anything it was gating blocks again.

    Corrections happen, and an unblock that survives its own cause is exactly the silent
    wrong state the design fears.
    """
    rows = conn.execute(
        "SELECT action_id FROM block WHERE kind='done' AND ref_kind=? AND ref_id=? AND satisfied=1",
        (ref_kind, ref_id),
    ).fetchall()
    conn.execute(
        "UPDATE block SET satisfied=0, satisfied_at=NULL, last_evaluated=? "
        "WHERE kind='done' AND ref_kind=? AND ref_id=? AND manual_override=0",
        (now(), ref_kind, ref_id),
    )
    return [r["action_id"] for r in rows]


def blocks_for(conn: sqlite3.Connection, action_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM block WHERE action_id=? ORDER BY created_at", (action_id,)
    ).fetchall()


def linked_open_actions(conn: sqlite3.Connection, ref_kind: str, ref_id: int) -> list[sqlite3.Row]:
    """Open actions that name (ref_kind, ref_id) in a `done` block — the dependents a caller
    closing that referent has to account for, whether it is deciding what to dispatch or what
    happens to the action once the referent is gone."""
    return conn.execute(
        """
        SELECT DISTINCT a.id, a.text FROM action a
        JOIN block b ON b.action_id = a.id
        WHERE b.kind='done' AND b.ref_kind=? AND b.ref_id=? AND a.state='open'
        """,
        (ref_kind, ref_id),
    ).fetchall()


def linked_action_ready(conn: sqlite3.Connection, waiting_id: int) -> bool:
    """Whether every action this waiting-for's own `done` block would release has no OTHER
    unsatisfied block standing in the way.

    A waiting-for with no linked action, or one whose only block is its own handoff, is
    trivially ready — the ordinary case, unaffected. One with a second unsatisfied block on
    the linked action — a judgment call, a date, another handoff — has nothing to hand the
    agent yet: dispatching the job would run it against a prerequisite that has not landed.
    """
    for row in linked_open_actions(conn, "waiting_for", waiting_id):
        other = conn.execute(
            "SELECT 1 FROM block WHERE action_id=? AND satisfied=0 "
            "AND NOT (kind='done' AND ref_kind='waiting_for' AND ref_id=?) LIMIT 1",
            (row["id"], waiting_id),
        ).fetchone()
        if other:
            return False
    return True


def block_age_days(row: sqlite3.Row, work: bool = False) -> int:
    try:
        created = date.fromisoformat((row["created_at"] or "")[:10])
    except ValueError:
        return 0
    if work:
        return business_days_between(created, date.today())
    return (date.today() - created).days


def is_work_record(conn: sqlite3.Connection, area_id: int | None,
                    project_id: int | None = None) -> bool:
    """True when the record's area — its own, or failing that its project's — is the Work
    area. See `config.WORK_AREA_NAME`."""
    if area_id is None and project_id is not None:
        row = conn.execute("SELECT area_id FROM project WHERE id=?", (project_id,)).fetchone()
        area_id = row["area_id"] if row else None
    if area_id is None:
        return False
    row = conn.execute("SELECT name FROM area WHERE id=?", (area_id,)).fetchone()
    return bool(row) and row["name"] == config.WORK_AREA_NAME
