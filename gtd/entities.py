"""The entity layer: identity plus layered attribute rows.

This shape is confined to entities for one reason — it is the only place three provenances
compete for the same field. An action's fields have a single author, so layering them would
be pure overhead and they stay columns.

A correction writes one more row rather than editing the others, and the effective value of
a field is a query: highest precedence wins, `asserted > inferred > source`. Precedence is a
property of the composition, not something the agent has to remember to apply, so the
correction is unskippable by construction — it is the top row of the stack the read already
composes. That is what puts this guarantee on the structural rung of the drift ladder.

The failure this exists to prevent: a recurring meeting the operator attends as an observer being
read as a participant slot every week, forever, because a memory is consulted if remembered
while a field is read because reading it is part of the procedure.
"""
from __future__ import annotations

import sqlite3

from .db import now

PRECEDENCE = {"source": 0, "inferred": 1, "asserted": 2}


def resolve(conn: sqlite3.Connection, entity_id: int) -> int:
    """Follow `merged_into` to the surviving entity. A duplicate-invite merge is an asserted
    identity fact, after which reads follow the pointer and the duplicate stops surfacing
    independently."""
    seen = set()
    current = entity_id
    while current is not None and current not in seen:
        seen.add(current)
        row = conn.execute("SELECT merged_into FROM entity WHERE id=?", (current,)).fetchone()
        if row is None or row["merged_into"] is None:
            return current
        current = row["merged_into"]
    return current


def compose(conn: sqlite3.Connection, entity_id: int, occurrence_key: str = "") -> dict[str, str]:
    """Effective field values.

    Two precedence axes, and their order matters. Provenance is primary — `asserted >
    inferred > source` — and the occurrence key only breaks ties *within* a layer. So a
    per-instance fact (this Wednesday's meeting is cancelled) beats the series-level source
    row, while an asserted correction still beats a source row of any scope.

    Ranking them the other way round would let the next calendar sync silently override a
    correction by writing an occurrence row, which is exactly the unskippability this layer
    exists to guarantee.
    """
    eid = resolve(conn, entity_id)
    rows = conn.execute(
        "SELECT field, value, provenance, occurrence_key FROM entity_attribute WHERE entity_id=?",
        (eid,),
    ).fetchall()
    best: dict[str, tuple[tuple[int, int], str]] = {}
    for r in rows:
        key = r["occurrence_key"] or ""
        if key not in ("", occurrence_key):
            continue
        rank = (PRECEDENCE.get(r["provenance"], 0), 1 if key else 0)
        prev = best.get(r["field"])
        if prev is None or rank > prev[0]:
            best[r["field"]] = (rank, r["value"])
    return {f: v for f, (_, v) in best.items()}


def get_or_create(conn: sqlite3.Connection, type_: str, identity_key: str,
                  vault_path: str | None = None) -> int:
    """Identity binds to `identity_key` — the calendar recurrence series for a meeting —
    never to a mutable attribute, so drift within the series updates the source layer and
    every asserted correction holds automatically."""
    row = conn.execute(
        "SELECT id FROM entity WHERE type=? AND identity_key=?", (type_, identity_key)
    ).fetchone()
    if row:
        return resolve(conn, row["id"])
    cur = conn.execute(
        "INSERT INTO entity (type, identity_key, vault_path, created_at) VALUES (?,?,?,?)",
        (type_, identity_key, vault_path, now()),
    )
    return int(cur.lastrowid)


def set_attr(conn: sqlite3.Connection, entity_id: int, field: str, value,
             provenance: str, occurrence_key: str = "") -> None:
    """Write one layer. Calendar sync only ever touches `source`, which is exactly what lets
    'presenter, no prep' survive the meeting moving to Wednesday while the time tracks it."""
    eid = resolve(conn, entity_id)
    conn.execute(
        "INSERT INTO entity_attribute (entity_id, field, value, provenance, occurrence_key, updated_at) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(entity_id, field, provenance, occurrence_key) DO UPDATE SET "
        "value=excluded.value, updated_at=excluded.updated_at",
        (eid, field, None if value is None else str(value), provenance, occurrence_key or "", now()),
    )


def materialise_inferred(conn: sqlite3.Connection, entity_id: int, fields: dict) -> int:
    """First-contact materialisation.

    Persist the agent's computed fields the first time it touches the entity, not the first
    time the operator corrects one. A right inference is lost just as surely as a wrong one if it is
    not written down, and re-inference can come out differently next week. Existing inferred
    rows are left alone — deciding once is the fix.
    """
    written = 0
    for field, value in fields.items():
        exists = conn.execute(
            "SELECT 1 FROM entity_attribute WHERE entity_id=? AND field=? AND provenance='inferred' "
            "AND occurrence_key=''",
            (resolve(conn, entity_id), field),
        ).fetchone()
        if exists:
            continue
        set_attr(conn, entity_id, field, value, "inferred")
        written += 1
    return written


def merge(conn: sqlite3.Connection, duplicate_id: int, survivor_id: int) -> None:
    conn.execute("UPDATE entity SET merged_into=? WHERE id=?", (survivor_id, duplicate_id))


def person_by_name(conn: sqlite3.Connection, name: str) -> int | None:
    key = name.strip().lower()
    if not key:
        return None
    row = conn.execute(
        "SELECT id FROM entity WHERE type='person' AND identity_key=?", (key,)
    ).fetchone()
    return resolve(conn, row["id"]) if row else None


def person(conn: sqlite3.Connection, name: str) -> int:
    key = name.strip().lower()
    eid = get_or_create(conn, "person", key)
    if not compose(conn, eid).get("name"):
        set_attr(conn, eid, "name", name.strip(), "source")
    return eid


# The runtimes the operator hands work to. An agent is not a person and the difference is load-bearing
# rather than tidy: the predicates that gate work on a person — is he about to see them, have
# they confirmed — have no meaning for a process, and a block built on one can never come
# true. The import had only 'person' to put an agent in, and a real deliverable sat hidden
# behind exactly that until it was found by lint.
AGENT_KEYS = frozenset({"claude", "codex", "agent-runtime", "opencode", "hermes",
                        "claude code", "claude desktop", "codex cli", "agent", "the agent"})


def is_agent_name(name: str | None) -> bool:
    return (name or "").strip().lower() in AGENT_KEYS


def agent(conn: sqlite3.Connection, name: str) -> int:
    key = name.strip().lower()
    eid = get_or_create(conn, "agent", key)
    if not compose(conn, eid).get("name"):
        set_attr(conn, eid, "name", name.strip(), "source")
    return eid


def entity_type(conn: sqlite3.Connection, entity_id: int | None) -> str | None:
    if entity_id is None:
        return None
    row = conn.execute("SELECT type FROM entity WHERE id=?", (entity_id,)).fetchone()
    return row["type"] if row else None
