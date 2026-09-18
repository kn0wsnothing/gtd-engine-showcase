"""The mutation surface. The agent is the sole caller.

Every mutation is one transaction, and transaction-time propagation rides inside it: what
does this unblock, and does this create new blocks for anything else? Scoped to the affected
records and cheap, so there is no reason to batch it — and no window in which an unblock is
visible without the completion that caused it.

Propagation keeps the graph true. Audit checks whether the graph was ever right. They are
different operations at different cadences, and only the first lives here.
"""
from __future__ import annotations

import difflib
import inspect
import json
import re
import sqlite3

from . import config, entities, journal, predicates
from .db import iso_week, now, today, tx


class GtdError(Exception):
    pass


def _named(conn: sqlite3.Connection, action_ids: list[int]) -> list[dict]:
    """Propagation results carrying the text that says what they are.

    What comes back from a completion gets read out loud to the operator — "finishing that just made
    these live" — and a bare id makes that sentence unspeakable, so the surface either drops
    the news or hands him a number to go look up. `predicates` stays graph-level and returns
    ids; naming happens at this layer, whose output a person reads.
    """
    out = []
    for aid in action_ids:
        row = conn.execute("SELECT text FROM action WHERE id=?", (aid,)).fetchone()
        out.append({"id": aid, "text": row["text"] if row else ""})
    return out


def _mark_moved(conn: sqlite3.Connection, kind: str, record_id: int) -> None:
    """A record changed, so its accumulated surfacings no longer count as 'shown and
    ignored'. This is what makes staleness mean surfaced-without-moving rather than merely
    surfaced."""
    conn.execute(
        "UPDATE surfacing_event SET moved=1 WHERE record_kind=? AND record_id=? AND moved=0",
        (kind, record_id),
    )


# --- capture and clarify ---------------------------------------------------------------

def capture(conn: sqlite3.Connection, raw: str, provenance: str, source: str = "",
            reason: str | None = None, line_hash: str | None = None) -> int:
    """Capture commits to nothing.

    An inbox item asserts only that something was said and should not be lost — raw,
    unprocessed, pre-clarify — so over-capturing is the safe direction to be wrong. The
    commitment decision is made at clarify, which is gated and the operator-owned.
    """
    with tx(conn):
        item_id = capture_row(conn, raw, provenance, source, reason, line_hash)
    journal.capture(raw, provenance, source)
    return item_id


def capture_row(conn: sqlite3.Connection, raw: str, provenance: str, source: str = "",
                reason: str | None = None, line_hash: str | None = None) -> int:
    """Write only the canonical capture row, joining the caller's transaction.

    Ordinary callers use :func:`capture`, which also appends the second-lineage journal.
    The transport gateway needs the row and its idempotency receipt in one transaction, so it
    composes this primitive with the journal append before that outer transaction commits.
    Keeping the insert here avoids a second implementation of canonical capture semantics.
    """
    if not provenance:
        raise GtdError("provenance is mandatory on capture")
    cur = conn.execute(
        "INSERT INTO inbox_item (raw, provenance, captured_at, reason) VALUES (?,?,?,?)",
        (raw, provenance, now(), reason),
    )
    item_id = int(cur.lastrowid)
    if line_hash:
        conn.execute(
            "INSERT OR IGNORE INTO ingest_ledger (line_hash, source, first_seen, inbox_item_id) "
            "VALUES (?,?,?,?)",
            (line_hash, source or provenance, now(), item_id),
        )
    return item_id


def draft_clarification(conn: sqlite3.Connection, item_id: int, draft: dict,
                        qc: dict | None = None, self_uncertainty: str | None = None,
                        ready: bool = False) -> None:
    """Clarify-prep's output: a proposal for the operator to react to, never a menu and never a
    question that requires him to produce the first draft.

    The store checks the draft against what it already holds and attaches any collision to the
    QC record. The run is told to look first, but an instruction the run can miss produces an
    invisible duplicate — two records for one thread, disagreeing about when it happens — and
    absence of a check leaves no trace anywhere. Attaching it here means a miss is still visible
    at the moment of commitment, which is the only place it can still be caught.
    """
    from . import queries

    # The drafted record may sit at the top level or nested under `fields`, and names its
    # project as either `project` or `project_id`. Read both rather than assuming one shape:
    # the whole point is that this check cannot be the thing that quietly does not run.
    body = draft.get("fields") if isinstance(draft.get("fields"), dict) else draft
    text = " ".join(str(body.get(k) or "") for k in ("text", "outcome", "expectation"))
    pid = body.get("project_id", body.get("project"))
    collisions = queries.similar_records(conn, text, pid if isinstance(pid, int) else None)
    if collisions:
        qc = dict(qc or {})
        qc["collisions"] = collisions

    # The same treatment for the field vocabulary itself. A draft is JSON that will be handed
    # to `clarify --fields` at the operator's gate, so a key that does not exist, or an `automated`
    # with no handoff on it, fails *there* — mid-review, in front of him — when the run that
    # wrote it was the one that could have fixed it. Attached rather than raised for the reason
    # the collision check is: the drafting run's output is still worth having, and a refusal
    # here would throw the whole draft away over a key.
    kind = str(draft.get("as") or draft.get("kind") or ("action" if body.get("text") else "") or "")
    if kind in CLARIFY_KINDS and kind != "nothing":
        checkable = {k: v for k, v in body.items() if k not in ("as", "kind")}
        try:
            normalize_clarify_fields(kind, checkable)
        except GtdError as exc:
            qc = dict(qc or {})
            qc["field_errors"] = str(exc)

    with tx(conn):
        conn.execute(
            "UPDATE inbox_item SET draft=?, qc=?, self_uncertainty=?, state=? WHERE id=?",
            (json.dumps(draft), json.dumps(qc) if qc else None, self_uncertainty,
             "clarify_ready" if ready else "captured", item_id),
        )


CLARIFY_KINDS = ("action", "project", "reference", "nothing")

# `clarify --fields` is JSON that lands as keyword arguments on the target constructor. A key
# that reads naturally to whoever drafted the batch — `project`, `type`, `work`, `exec` —
# reaches Python as an unexpected keyword and raises TypeError mid-batch, at the moment of
# commitment, in front of the operator. The drafting jobs reach for the flag names `gtd add` already
# uses, so those are the aliases: the shorthand was never wrong, it was only ever taught to
# one of the two doors into the same function.
_CLARIFY_ALIASES: dict[str, dict[str, str]] = {
    "action": {"project": "project_id", "area": "area_id", "type": "commitment_type",
               "work": "work_type", "exec": "execution_type", "execution": "execution_type",
               "block": "blocks", "handoff": "handoff_expectation",
               "expectation": "handoff_expectation"},
    "project": {"area": "area_id", "test": "done_test", "path": "vault_path"},
    "reference": {"relates": "relates_kind", "relates_to": "relates_kind"},
    "nothing": {},
}

# Set by clarify itself — a caller passing one would collide with the call it makes.
_CLARIFY_RESERVED = frozenset({"conn", "item_id", "kind", "inbox_item_id", "source_kind"})

# The handoff, drafted and committed in the same motion as the action it suppresses.
#
# These are not fields of `add_action` — they are a second record — and for two days that was
# the whole bug. `automated` said the labour was not the operator's, and creating the `waiting_for` that
# says so was left to whoever remembered to make a second call. Nobody did: `gtd dispatch` ran
# 294 times and queued nothing, because no waiting-for in the store had ever carried a lane.
# Putting them in clarify's vocabulary is what makes "automated" and "has a dispatchable
# handoff" structurally unable to disagree, rather than a warning after the fact.
_HANDOFF_FIELDS = ("handoff_expectation", "lane", "brief")

# `reference` is an INSERT rather than a constructor, so its vocabulary is named here.
_REFERENCE_FIELDS = frozenset({"path", "why", "relates_kind", "relates_id"})


def _clarify_params(kind: str) -> dict[str, inspect.Parameter]:
    """Read the target's accepted fields off the function itself, so this cannot drift out of
    step with a signature someone changes later."""
    if kind == "reference":
        return {n: inspect.Parameter(n, inspect.Parameter.KEYWORD_ONLY, default=None)
                for n in _REFERENCE_FIELDS}
    if kind == "nothing":
        return {}
    target = add_action if kind == "action" else add_project
    params = {n: p for n, p in inspect.signature(target).parameters.items()
              if n not in _CLARIFY_RESERVED}
    if kind == "action":
        params.update({
            n: inspect.Parameter(n, inspect.Parameter.KEYWORD_ONLY, default=None)
            for n in _HANDOFF_FIELDS})
    return params


def normalize_clarify_fields(kind: str, fields: dict) -> dict:
    """Map the drafting vocabulary onto the constructor's, and refuse an unknown key by name.

    The point is the error, not the aliasing. A batch of clarifications is committed one call
    at a time, so an unnamed TypeError halfway through leaves half the batch applied and says
    only that *something* was an unexpected keyword. Naming the key, and what it should have
    been, is the difference between a typo and an interrupted review.
    """
    if kind not in CLARIFY_KINDS:
        raise GtdError(f"unknown clarify target {kind!r}")
    params = _clarify_params(kind)
    aliases = _CLARIFY_ALIASES[kind]
    accepted = set(params)

    out: dict = {}
    for raw_key, value in (fields or {}).items():
        key = str(raw_key).strip().replace("-", "_")
        key = aliases.get(key, key)
        if key in _CLARIFY_RESERVED:
            raise GtdError(f"{raw_key!r} is set by clarify itself and cannot be passed in --fields")
        if key not in accepted:
            if kind == "nothing":
                raise GtdError("`clarify --as nothing` records that a capture became no "
                               f"commitment — it takes no fields, but got {raw_key!r}")
            suggestion = difflib.get_close_matches(key, sorted(accepted | set(aliases)), n=1)
            shorthand = ", ".join(f"{a} → {t}" for a, t in sorted(aliases.items()))
            raise GtdError(
                f"unknown field {raw_key!r} for `clarify --as {kind}`."
                + (f" Did you mean {suggestion[0]!r}?" if suggestion else "")
                + f" Accepted: {', '.join(sorted(accepted))}."
                + (f" Shorthand also accepted: {shorthand}." if shorthand else ""))
        if key in out:
            raise GtdError(f"--fields sets {key!r} twice under different names")
        out[key] = value

    missing = {n for n, p in params.items() if p.default is inspect.Parameter.empty} - set(out)
    if missing:
        raise GtdError(f"`clarify --as {kind}` needs {', '.join(sorted(missing))} in --fields")
    if kind == "action":
        _check_handoff_coherence(out)
    return out


def _check_handoff_coherence(fields: dict) -> None:
    """`automated` and "has a handoff" are one decision, so they are refused separately.

    This is the check that makes the `automated-unattached` lint finding unreachable for a new
    record instead of merely reported on 46 old ones. An automated action with nothing attached
    is not a near-miss: it stays unblocked, so `gtd next` returns it and the morning surface
    offers the operator the one category of work that was never his.
    """
    handoff = {n: fields.get(n) for n in _HANDOFF_FIELDS}
    given = {n for n, v in handoff.items() if str(v or "").strip()}
    execution = fields.get("execution_type")

    if execution == "automated" and not handoff["handoff_expectation"]:
        raise GtdError(
            "`execution_type=automated` says the labour is not the operator's, so the record has to say "
            "who holds it: pass `handoff_expectation` (what the agent owes) and, when an "
            "unattended run can execute it, `lane` and `brief`. Without one the action stays "
            "unblocked and the morning surface offers it to the operator as ripe work. If the missing "
            "input is a decision, an approval or a preference, this is `interactive` however "
            "the text is phrased.")
    if given and execution != "automated":
        named = ", ".join(sorted(given))
        raise GtdError(
            f"{named} describes work handed to an agent, but execution_type="
            f"{execution!r}. A handoff on interactive work is a contradiction — either the "
            "labour is the agent's, and this is `exec: automated`, or it is the operator's, and the "
            "handoff fields come off.")
    if given and not handoff["handoff_expectation"]:
        named = ", ".join(sorted(given - {"handoff_expectation"}))
        raise GtdError(
            f"{named} needs a `handoff_expectation` — the one line naming what the agent owes, "
            "which is what the operator reads in a review and what `gtd waiting` shows.")
    if handoff["lane"] and not str(handoff["brief"] or "").strip():
        raise GtdError(
            "a lane needs a brief: the expectation is a one-line summary for the operator to read in a "
            "review, not instructions an unattended run can execute from")
    if execution == "automated" and not handoff["lane"]:
        raise GtdError(
            "`execution_type=automated` also needs `lane` (and `brief`) on the handoff — "
            "without them `gtd dispatch` has nothing to execute, and the action sits blocked "
            "until someone notices and backfills both by hand")


def clarify(conn: sqlite3.Connection, item_id: int, kind: str, reason: str = "",
            force: bool = False, authorized_by: str = "", decision: str = "", **fields) -> int:
    """Turn a raw capture into a commitment, a reference, or nothing.

    The raw item is archived, never destroyed: a wrong capture costs nothing and a wrong
    clarification can roll back to its source.

    `reason` is a property of the decision rather than of the record it produces, so it is not
    part of the field vocabulary. It is mandatory for `--as nothing`, which is the one target
    that asserts a capture was never a commitment and the only one that leaves nothing behind
    to argue with. `force` overrides the same-session self-disposal refusal below.

    `authorized_by` and `decision` are the same property in the other direction: proof that a
    capture the agent itself proposed became a commitment because the operator said so, not because the
    run that drafted it also clarified it. See `_check_agent_proposal_authorization`.

    An action carrying handoff fields creates its `waiting_for` in the same transaction. The
    two records are one decision and a partial write of it is the defect: an action written
    without its handoff is unblocked automated work, which is what the whole automated lane
    exists to keep off the operator's surface.
    """
    fields = normalize_clarify_fields(kind, fields)
    item = conn.execute("SELECT * FROM inbox_item WHERE id=?", (item_id,)).fetchone()
    if item is None:
        raise GtdError(f"inbox item #{item_id} not found")
    if kind == "nothing":
        _check_nothing_is_defensible(item, reason, force)
    proposal = kind in ("action", "project") and _is_agent_proposal(item)
    if proposal:
        _check_agent_proposal_authorization(item, authorized_by, decision)

    handoff = {n: fields.pop(n, None) for n in _HANDOFF_FIELDS} if kind == "action" else {}
    result_id = 0
    waiting_id = None
    with tx(conn):
        if kind == "action":
            result_id = add_action(conn, inbox_item_id=item_id, source_kind="clarify", **fields)
            if handoff.get("handoff_expectation"):
                waiting_id = add_waiting(
                    conn, config.DEFAULT_AGENT, handoff["handoff_expectation"],
                    is_agent=True, blocks_action_id=result_id,
                    lane=handoff.get("lane"), brief=handoff.get("brief"))
        elif kind == "project":
            result_id = add_project(conn, **fields)
        elif kind == "reference":
            cur = conn.execute(
                "INSERT INTO reference (path, why, relates_kind, relates_id, inbox_item_id, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (fields.get("path", ""), fields.get("why"), fields.get("relates_kind"),
                 fields.get("relates_id"), item_id, now()),
            )
            result_id = int(cur.lastrowid)

        conn.execute(
            "UPDATE inbox_item SET state=?, clarified_at=?, result_kind=?, result_id=? WHERE id=?",
            ("reference" if kind == "reference" else "clarified", now(), kind, result_id, item_id),
        )
    # Outside the transaction, and after it: the journal is a file in the vault, and an entry
    # written for a clarification that then rolled back would be a lie in the rebuild source.
    journal.clarification(item_id, item["raw"], kind, result_id, reason=reason,
                          waiting_for=waiting_id, lane=handoff.get("lane"),
                          note="forced over the fresh-capture refusal" if (force and kind == "nothing") else "",
                          authorized_by=authorized_by if proposal else "",
                          decision=decision if proposal else "")
    return result_id


_AGENT_STAMP_RE = re.compile(r"(?:^|\s)agent=\S+")


def _is_agent_proposal(item: sqlite3.Row) -> bool:
    """An agent's own suggestion, marked as the operator's call rather than the operator's decision.

    Neither half alone says that: an `agent=` provenance stamp on an otherwise ordinary
    capture is just an agent relaying something the operator said, and `[john-call]` on a capture
    the operator typed himself is only him flagging his own note. Together they are the shape 26
    one-off technical suggestions took on 2026-08-20 — `[john-call] I would scope it...`,
    `I would do it...`, `I would add...` — proposals that read as decided because nothing
    distinguished them from a capture of something the operator had actually said.
    """
    from .morning import OPERATOR_CALL_MARKER

    provenance = str(item["provenance"] or "")
    raw = str(item["raw"] or "").strip().lower()
    return bool(_AGENT_STAMP_RE.search(provenance)) and raw.startswith(OPERATOR_CALL_MARKER)


def _check_agent_proposal_authorization(item: sqlite3.Row, authorized_by: str,
                                        decision: str) -> None:
    """The store-level proof that an agent-authored `[john-call]` capture became a commitment
    because the operator accepted it, not because the run that drafted it also clarified it.

    The prompt rules already say an agent's suggestion is not a commitment, but nothing at the
    mutation boundary ever checked — `gtd clarify` took the same `--fields` for a `[john-call]`
    proposal as for anything else, and 26 of them became live actions and projects that way.
    `authorized_by` has to be the literal `john`, not a truthy flag, and `decision` has to say
    what he accepted, not merely that something was accepted.
    """
    if authorized_by != "john":
        raise GtdError(
            f"inbox item #{item['id']} is an agent-authored [john-call] capture — the agent "
            "proposed it, and a proposal is not a commitment until the operator says so. Clarifying it "
            "as an action or project needs `--authorized-by john` naming who accepted it.")
    if not (decision or "").strip():
        raise GtdError(
            f"inbox item #{item['id']} is an agent-authored [john-call] capture — "
            "`--authorized-by john` also needs `--decision` recording what he accepted, in his "
            "own words, or the authorization is a flag with nothing behind it.")


def _check_nothing_is_defensible(item: sqlite3.Row, reason: str, force: bool) -> None:
    """`--as nothing` destroys a commitment, and it was the cheapest disposition to record.

    Thirteen `[agent-fix]` retro captures went this way in two days, every one with
    `reason: None`, and four of them were disposed of 1–6 minutes after being captured — a
    session wrapping up and quietly clearing the defects it had just written down. Nothing in
    the record distinguishes that from a deliberate sweep at a review, which is why the thirteen
    could not simply be resurrected afterwards.

    So: a reason is mandatory, and a capture younger than the window is refused outright. The
    override exists because a genuinely instant "that was already handled" does happen — but it
    has to be stated, and it is journalled as an override.
    """
    from datetime import datetime

    if not (reason or "").strip():
        raise GtdError(
            "`clarify --as nothing` asserts this was never a commitment, which is the one "
            "disposition that leaves nothing behind to argue with — pass --reason saying why. "
            "Work that was completed is a done action, not nothing; work already tracked "
            "elsewhere names the record it duplicates.")
    if force:
        return
    captured_at = item["captured_at"]
    try:
        age_min = (datetime.now() - datetime.fromisoformat(captured_at)).total_seconds() / 60
    except (TypeError, ValueError):
        return
    if age_min < config.SELF_CLARIFY_WINDOW_MIN:
        raise GtdError(
            f"inbox item #{item['id']} was captured {int(age_min)} minutes ago "
            f"({captured_at}) and has not been through a clarify-prep draft or a review — "
            "disposing of it now is the same session capturing something and destroying it in "
            "the same wrap, which is how thirteen retro fixes were lost on 2026-07-28. Leave it "
            "for the next drafting run and the operator's gate. If it truly was never a commitment, "
            "pass --force and the override is journalled.")


# --- actions ----------------------------------------------------------------------------

def add_action(conn: sqlite3.Connection, text: str, project_id: int | None = None,
               area_id: int | None = None, commitment_type: str | None = None,
               work_type: str | None = None, execution_type: str | None = None,
               person: str | None = None, hard_date: str | None = None,
               plan_date: str | None = None, commit_week: str | None = None,
               state: str = "open", source_kind: str = "manual",
               source_path: str | None = None, source_line: int | None = None,
               inbox_item_id: int | None = None, legacy_id: str | None = None,
               blocks: list[dict] | None = None) -> int:
    for name, value, allowed in (
        ("commitment_type", commitment_type, config.COMMITMENT_TYPES),
        ("work_type", work_type, config.WORK_TYPES),
        ("execution_type", execution_type, config.EXECUTION_TYPES),
    ):
        if value is not None and value not in allowed:
            raise GtdError(f"{name}={value!r} outside closed vocabulary {allowed}")

    with tx(conn):
        person_id = entities.person(conn, person) if person else None
        cur = conn.execute(
            """INSERT INTO action
               (text, project_id, area_id, commitment_type, work_type, execution_type,
                person_id, hard_date, plan_date, commit_week, state, created_at,
                source_kind, source_path, source_line, inbox_item_id, legacy_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (text, project_id, area_id, commitment_type, work_type, execution_type,
             person_id, hard_date, plan_date, commit_week, state, now(),
             source_kind, source_path, source_line, inbox_item_id, legacy_id),
        )
        action_id = int(cur.lastrowid)
        if isinstance(blocks, dict):
            blocks = [blocks]
        for b in (blocks or []):
            _insert_block(conn, action_id, b)
    if inbox_item_id is None:
        # An action born from a capture already has its raw line in the journal; clarify only
        # shapes it. One that never passed through the inbox — a direct `gtd add`, or the
        # one-shot import — has no entry at all, and would vanish from this lineage entirely.
        journal.added("action", action_id, text,
                      provenance=source_path or source_kind)
    return action_id


def _insert_block(conn: sqlite3.Connection, action_id: int, spec: dict) -> int:
    kind = spec.get("kind")
    if kind not in config.BLOCK_KINDS:
        raise GtdError(f"block kind {kind!r} outside closed vocabulary {config.BLOCK_KINDS}")
    entity_id = spec.get("ref_entity_id")
    if entity_id is None and spec.get("person"):
        name = spec["person"]
        # Naming an agent here used to mint a person record for it, which is how an agent
        # became one. Route it to its own type instead — and then refuse the block, because
        # the two entity-shaped predicates are both about the operator's access to a human.
        entity_id = (entities.agent(conn, name) if entities.is_agent_name(name)
                     else entities.person(conn, name))
    if kind in ("proximity", "confirmed") and entities.entity_type(conn, entity_id) == "agent":
        raise GtdError(
            f"a {kind} block cannot wait on an agent: proximity means the operator is about to see "
            "someone and confirmation means they replied, and neither ever becomes true for a "
            "process. If the work is the agent's, record it as an agent waiting-for and hang "
            "the action off that with `gtd waiting-add <agent> \"<what is owed>\" --agent "
            "--blocks <action>`")
    cur = conn.execute(
        """INSERT INTO block
           (action_id, kind, ref_kind, ref_id, ref_date, ref_path, ref_entity_id, prose, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (action_id, kind, spec.get("ref_kind"), spec.get("ref_id"), spec.get("ref_date"),
         spec.get("ref_path"), entity_id, spec.get("prose"), now()),
    )
    return int(cur.lastrowid)


def add_block(conn: sqlite3.Connection, action_id: int, spec: dict) -> int:
    with tx(conn):
        bid = _insert_block(conn, action_id, spec)
        _mark_moved(conn, "action", action_id)
    predicates.refresh_mechanical(conn)
    return bid


def unblock(conn: sqlite3.Connection, block_id: int, manual: bool = True) -> list[int]:
    """Flip a block satisfied. `manual` makes it sticky against mechanical recomputation —
    'I'm about to call a person' is a fact the calendar does not know."""
    with tx(conn):
        row = conn.execute("SELECT * FROM block WHERE id=?", (block_id,)).fetchone()
        if row is None:
            raise GtdError(f"block #{block_id} not found")
        conn.execute(
            "UPDATE block SET satisfied=1, satisfied_at=?, last_evaluated=?, manual_override=? "
            "WHERE id=?",
            (now(), now(), 1 if manual else 0, block_id),
        )
        _mark_moved(conn, "action", row["action_id"])
        act = conn.execute("SELECT text FROM action WHERE id=?", (row["action_id"],)).fetchone()
        action_id = row["action_id"]
    if manual:
        # A mechanical unblock is recomputed on every refresh, so it is derivable and stays out.
        # A manual one is the opposite: "I'm about to call a person" is a fact nothing else knows,
        # and it is sticky against recomputation precisely because it cannot be re-derived.
        journal.closure("block", block_id, "satisfied",
                        text=act["text"] if act else "", note="manual override")
    return _named(conn, [action_id])


def remove_block(conn: sqlite3.Connection, block_id: int) -> None:
    with tx(conn):
        conn.execute("DELETE FROM block WHERE id=?", (block_id,))


def done(conn: sqlite3.Connection, action_id: int, when: str | None = None) -> dict:
    """Close an action and propagate in the same transaction.

    An unrecorded completion actively suppresses the wrong things — the action it should
    have unblocked stays invisible, and the failure is undetectable, because invisible is
    exactly what a blocked item looks like. So propagation is never deferred to a sweep.
    """
    with tx(conn):
        row = conn.execute("SELECT * FROM action WHERE id=?", (action_id,)).fetchone()
        if row is None:
            raise GtdError(f"action #{action_id} not found")
        if row["state"] == "done":
            return {"action": action_id, "text": row["text"], "unblocked": [], "already": True}
        conn.execute(
            "UPDATE action SET state='done', done_at=?, closed_at=? WHERE id=?",
            (when or today(), now(), action_id),
        )
        _mark_moved(conn, "action", action_id)
        unblocked = predicates.propagate_done(conn, "action", action_id)
    journal.closure("action", action_id, "done", text=row["text"])
    return {"action": action_id, "text": row["text"], "unblocked": _named(conn, unblocked),
            "already": False}


def drop(conn: sqlite3.Connection, action_id: int, reason: str = "") -> dict:
    with tx(conn):
        row = conn.execute("SELECT * FROM action WHERE id=?", (action_id,)).fetchone()
        if row is None:
            raise GtdError(f"action #{action_id} not found")
        conn.execute(
            "UPDATE action SET state='dropped', closed_at=?, reason=? WHERE id=?",
            (now(), reason, action_id))
        _mark_moved(conn, "action", action_id)
        unblocked = predicates.propagate_done(conn, "action", action_id)
    journal.closure("action", action_id, "dropped", text=row["text"], reason=reason)
    return {"action": action_id, "text": row["text"], "unblocked": _named(conn, unblocked),
            "reason": reason}


def reopen(conn: sqlite3.Connection, action_id: int) -> dict:
    with tx(conn):
        row = conn.execute("SELECT * FROM action WHERE id=?", (action_id,)).fetchone()
        if row is None:
            raise GtdError(f"action #{action_id} not found")
        conn.execute(
            "UPDATE action SET state='open', done_at=NULL, closed_at=NULL WHERE id=?", (action_id,))
        reblocked = predicates.propagate_reopen(conn, "action", action_id)
    journal.closure("action", action_id, "open", text=row["text"], note="reopened")
    return {"action": action_id, "text": row["text"], "reblocked": _named(conn, reblocked)}


def park(conn: sqlite3.Connection, action_id: int) -> dict:
    """Someday: invisible to surfacing, visible at the monthly."""
    with tx(conn):
        row = conn.execute("SELECT * FROM action WHERE id=?", (action_id,)).fetchone()
        conn.execute("UPDATE action SET state='someday' WHERE id=?", (action_id,))
        _mark_moved(conn, "action", action_id)
    journal.closure("action", action_id, "someday", text=row["text"] if row else "")
    return {"action": action_id, "text": row["text"] if row else "", "state": "someday"}


def reactivate(conn: sqlite3.Connection, action_id: int) -> dict:
    with tx(conn):
        row = conn.execute("SELECT * FROM action WHERE id=?", (action_id,)).fetchone()
        conn.execute("UPDATE action SET state='open' WHERE id=?", (action_id,))
        _mark_moved(conn, "action", action_id)
    journal.closure("action", action_id, "open", text=row["text"] if row else "",
                    note="reactivated from someday")
    return {"action": action_id, "text": row["text"] if row else "", "state": "open"}


def commit_to_week(conn: sqlite3.Connection, action_id: int, week: str | None = None) -> None:
    """The deterministic bridge from a weekly Big 3 down to the daily surface. A store field
    now, not a tag edit."""
    with tx(conn):
        conn.execute("UPDATE action SET commit_week=? WHERE id=?", (week or iso_week(), action_id))
        _mark_moved(conn, "action", action_id)


def defer(conn: sqlite3.Connection, action_id: int, until: str) -> int:
    """A defer is a `date_reached` block, not a status. The visibility gate and the hard
    commitment are different fields and never collapse into one."""
    return add_block(conn, action_id, {"kind": "date_reached", "ref_date": until})


# --- projects and areas ------------------------------------------------------------------

def add_project(conn: sqlite3.Connection, outcome: str, done_test: str | None = None,
                area_id: int | None = None, state: str = "active",
                vault_path: str | None = None, legacy_id: str | None = None) -> int:
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO project (outcome, done_test, area_id, state, vault_path, created_at, legacy_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (outcome, done_test, area_id, state, vault_path, now(), legacy_id))
        project_id = int(cur.lastrowid)
    journal.added("project", project_id, outcome, provenance=vault_path or "")
    return project_id


def close_project(conn: sqlite3.Connection, project_id: int, state: str = "done") -> dict:
    """Archiving is a move, not a loss: the project record persists with its pointers
    intact, so its history stays readable."""
    closed_at = now()
    with tx(conn):
        row = conn.execute("SELECT * FROM project WHERE id=?", (project_id,)).fetchone()
        conn.execute(
            "UPDATE project SET state=?, closed_at=? WHERE id=?", (state, closed_at, project_id))
        _mark_moved(conn, "project", project_id)
        unblocked = predicates.propagate_done(conn, "project", project_id)
    journal.closure("project", project_id, state, text=row["outcome"] if row else "")
    block = None
    if row and row["vault_path"]:
        # The projection follows the state change immediately rather than waiting for the
        # next `gtd render` — a paused project's block otherwise strands showing whatever it
        # last rendered while active, forever, since a non-active project leaves the set
        # `render_all` visits by design.
        from . import render
        block = render.render_project_terminal(
            conn, project_id, state, row["vault_path"], closed_at=closed_at)
    return {"project": project_id, "text": row["outcome"] if row else "",
            "state": state, "unblocked": unblocked, "block": block}


def add_area(conn: sqlite3.Connection, name: str, standard: str | None = None,
             vault_path: str | None = None) -> int:
    with tx(conn):
        conn.execute(
            "INSERT INTO area (name, standard, vault_path, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "standard=COALESCE(excluded.standard, area.standard), "
            "vault_path=COALESCE(excluded.vault_path, area.vault_path)",
            (name, standard, vault_path, now()))
        row = conn.execute("SELECT id FROM area WHERE name=?", (name,)).fetchone()
        return int(row["id"])


# --- waiting-for --------------------------------------------------------------------------

def add_waiting(conn: sqlite3.Connection, counterparty: str, expectation: str,
                expected_by: str | None = None, project_id: int | None = None,
                is_agent: bool = False, source_path: str | None = None,
                legacy_id: str | None = None, blocks_action_id: int | None = None,
                lane: str | None = None, brief: str | None = None) -> int:
    """Record that something is owed, and optionally that an action waits on it.

    `blocks_action_id` is what keeps an automated action off the operator's morning surface. Without
    it, handing work to an agent left the action open and unblocked, so `gtd next` returned
    it as ripe — the system asking the operator to do the thing whose whole point was that he would
    not. The link is the ordinary `done(waiting_for:N)` block rather than a new column, so
    the existing unblocked predicate suppresses it and `close_waiting` releases it. One
    authority for blocking, not two.
    """
    if not counterparty.strip():
        raise GtdError("a waiting-for needs a named counterparty — the record has to say who to chase")
    if not expectation.strip():
        raise GtdError("a waiting-for needs a stated expectation — the record has to say what "
                       "is owed, or a chase has nothing to say")
    if lane is not None:
        if lane not in config.LANES:
            raise GtdError(f"lane={lane!r} outside closed vocabulary {config.LANES}")
        if not is_agent:
            raise GtdError("a lane says how an unattended run executes this, so it belongs "
                           "only on agent work — pass --agent, or drop the lane")
        if not (brief or "").strip():
            # A lane without a brief dispatches an empty prompt. Better to refuse at the point
            # the handoff is written, while whoever knows the task is still here.
            raise GtdError("a lane needs a brief: the expectation is a one-line summary for "
                           "the operator to read in a review, not instructions an unattended run can "
                           "execute from")
        if lane == "code":
            # The allowlist is an authority boundary, so refuse a new direct handoff while the
            # caller is still present instead of letting it become a poison record in the next
            # dispatch pass. Dispatch repeats this check for legacy and corrected records.
            from . import codelane
            codelane.resolve_brief_repo(brief or "")
    with tx(conn):
        if blocks_action_id is not None:
            act = conn.execute(
                "SELECT id, execution_type FROM action WHERE id=?",
                (blocks_action_id,)).fetchone()
            if act is None:
                raise GtdError(f"no action #{blocks_action_id} to block on this waiting-for")
            if is_agent and act["execution_type"] == "automated" and not lane:
                # The same coherence `_check_handoff_coherence` enforces at clarify, for every
                # other caller — `gtd waiting-add --blocks` reaches this constructor directly
                # and clarify's field check never runs, so the guarantee has to live here too.
                raise GtdError(
                    "this waiting-for blocks automated work, so it also needs `lane` (and "
                    "`brief`) — without them the action can never actually be dispatched, and "
                    "it sits blocked until someone notices and backfills both by hand")
        # An agent counterparty gets a real record of its own type rather than a null. The
        # flag alone said "not a person" without saying what it was, so nothing downstream
        # could ask the store whether Claude and claude were the same counterparty.
        cp_id = (entities.agent(conn, counterparty)
                 if is_agent or entities.is_agent_name(counterparty)
                 else entities.person(conn, counterparty))
        cur = conn.execute(
            """INSERT INTO waiting_for
               (counterparty, counterparty_id, expectation, expected_by, project_id,
                is_agent, created_at, source_path, legacy_id, lane, brief)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (counterparty, cp_id, expectation, expected_by, project_id,
             1 if is_agent else 0, now(), source_path, legacy_id, lane, brief))
        waiting_id = int(cur.lastrowid)
        if blocks_action_id is not None:
            _insert_block(conn, blocks_action_id,
                          {"kind": "done", "ref_kind": "waiting_for", "ref_id": waiting_id})
            _mark_moved(conn, "action", blocks_action_id)
        return waiting_id


def close_waiting(conn: sqlite3.Connection, waiting_id: int, state: str = "delivered",
                   reason: str = "", dependent_actions: str | None = None) -> dict:
    """Close a handoff. `dropped` is the one state that can silently manufacture a task: an
    open action's own `done(waiting_for:N)` block names this record, so dropping it releases
    that action into the gate as ripe work — even when the reason this route died is that the
    action itself no longer matters (waiting-for 197's obsolete audit released action 532,
    "Review the pre-landing-gate audit...", into the gate although the audit it reviewed had
    itself been retired).

    So a `dropped` close on a waiting-for with an open dependent needs `dependent_actions`:
    `release` keeps today's propagation (the action still matters, only this route was
    withdrawn) or `drop` closes the dependent with it, carrying this reason into its own
    closure. Neither is assumed — an unnamed disposition is refused before anything is written,
    naming the actions it would otherwise have silently decided for.

    `delivered` and `converted` are unchanged: they always propagate, exactly as before.
    """
    dropped_dependents: list[sqlite3.Row] = []
    with tx(conn):
        dependents = predicates.linked_open_actions(conn, "waiting_for", waiting_id) \
            if state == "dropped" else []
        if dependents and dependent_actions not in config.DEPENDENT_ACTION_DISPOSITIONS:
            named = "; ".join(f"#{a['id']} ({a['text']})" for a in dependents)
            raise GtdError(
                f"waiting-for #{waiting_id} still blocks {named} through its own "
                "done(waiting_for) block. Dropping it needs `--dependent-actions release` "
                "(the action still matters, only this route was withdrawn) or "
                "`--dependent-actions drop` (the action goes with it) — leaving it unnamed "
                "would silently decide which, and either wrong is a real cost.")

        conn.execute(
            "UPDATE waiting_for SET state=?, closed_at=?, reason=? WHERE id=?",
            (state, now(), reason, waiting_id))
        _mark_moved(conn, "waiting_for", waiting_id)

        if dependents and dependent_actions == "drop":
            unblocked: list[int] = []
            for a in dependents:
                conn.execute(
                    "UPDATE action SET state='dropped', closed_at=?, reason=? WHERE id=?",
                    (now(), reason, a["id"]))
                _mark_moved(conn, "action", a["id"])
                unblocked += predicates.propagate_done(conn, "action", a["id"])
                dropped_dependents.append(a)
        else:
            unblocked = predicates.propagate_done(conn, "waiting_for", waiting_id)
    for a in dropped_dependents:
        journal.closure("action", a["id"], "dropped", text=a["text"], reason=reason,
                        note=f"dropped with waiting-for #{waiting_id}")
    out = {"waiting_for": waiting_id, "unblocked": _named(conn, unblocked), "reason": reason}
    if dropped_dependents:
        out["dropped_dependents"] = [a["id"] for a in dropped_dependents]
    return out


def chase(conn: sqlite3.Connection, waiting_id: int) -> None:
    """Repeat waits escalate rather than reset. The counter is visible on purpose: chronic
    waiting usually means the dependency is wrong rather than that the person is slow."""
    with tx(conn):
        conn.execute(
            "UPDATE waiting_for SET chase_count=chase_count+1, ripe_cycles=ripe_cycles+1 WHERE id=?",
            (waiting_id,))


# --- corrections, surfacing, rejections ---------------------------------------------------

def _check_no_verb_advance(row: sqlite3.Row, new_text, force: bool) -> str:
    """`correct` fixes what a record should always have said; it never advances a finished
    job to its next step by rewriting the same record. A record whose leading verb changes
    right after it was created, or right after it was closed done, is that move wearing a
    correction's clothes — see docs/DECISIONS.md for the incident this guards against
    (action 357: a completed build was rewritten into a review, with no `gtd done` and no new
    record, so the finished build reappeared in Focus as if nothing had happened).

    60 minutes, not the round 24h a first pass might reach for: classified against the 30
    live `action:text` corrections on 2026-08-03, the fastest *legitimate* verb-changing
    correction landed ~4h50m after its record's creation, and every other one took a day or
    more. A 24h window would have refused that legitimate one; 60 minutes refuses only the
    pattern this guard exists for.

    Returns a note to journal when `force` overrode a trigger; empty string when nothing
    applied.
    """
    def _leading_verb(text: str) -> str:
        words = re.findall(r"[a-z0-9'\-]+", (text or "").lower())
        return words[0] if words else ""

    old_verb = _leading_verb(row["text"])
    new_verb = _leading_verb(str(new_text) if new_text is not None else "")
    if not old_verb or not new_verb or old_verb == new_verb:
        return ""

    from datetime import datetime

    def _age_minutes(ts):
        if not ts:
            return None
        try:
            return (datetime.now() - datetime.fromisoformat(ts)).total_seconds() / 60
        except ValueError:
            return None

    window = config.TEXT_VERB_ADVANCE_WINDOW_MIN
    triggers = []
    if row["state"] == "done":
        age = _age_minutes(row["closed_at"])
        if age is not None and age < window:
            triggers.append(f"closed done {age:.0f} min ago")
    age_created = _age_minutes(row["created_at"])
    if age_created is not None and age_created < window:
        triggers.append(f"created {age_created:.0f} min ago")
    if not triggers:
        return ""

    trigger_str = " and ".join(triggers)
    if force:
        return f"forced past the verb-advance guard ({trigger_str}; {old_verb!r} -> {new_verb!r})"
    raise GtdError(
        f"action #{row['id']} was {trigger_str}, and this correction changes its leading verb "
        f"({old_verb!r} -> {new_verb!r}) — a finished job is closed with `gtd done` and its "
        "successor added as its own action with `gtd add`, never advanced by rewriting this "
        "record's text (docs/DECISIONS.md). If this really is the same job and the wording "
        "was simply wrong, pass --force; the override is journalled.")


def correct(conn: sqlite3.Connection, record_kind: str, record_id: int, field: str,
            new_value, reason: str = "", force: bool = False) -> int:
    """A correction is durable only if it attaches to a specific record as a specific field.

    On an entity it also writes the asserted attribute row the read composes, which is what
    makes it unskippable rather than a memory the agent has to decide to consult.
    """
    table = {"action": "action", "project": "project", "area": "area",
             "waiting_for": "waiting_for", "entity": "entity",
             "rejection": "rejection"}.get(record_kind)
    if table is None:
        raise GtdError(f"unknown record kind {record_kind!r}")

    label = ""
    note = ""
    with tx(conn):
        old = None
        if record_kind == "entity":
            old = entities.compose(conn, record_id).get(field)
            entities.set_attr(conn, record_id, field, new_value, "asserted")
        else:
            row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise GtdError(f"{record_kind} #{record_id} not found")
            if field not in row.keys():
                raise GtdError(f"{record_kind} has no field {field!r}")
            if record_kind == "action" and field == "text":
                note = _check_no_verb_advance(row, new_value, force)
            old = row[field]
            for candidate in ("text", "outcome", "name", "expectation", "raw_text"):
                if candidate in row.keys() and row[candidate]:
                    label = str(row[candidate])
                    break
            try:
                conn.execute(f"UPDATE {table} SET {field}=? WHERE id=?", (new_value, record_id))
            except sqlite3.IntegrityError as exc:
                if new_value is not None:
                    raise GtdError(
                        f"{record_kind} #{record_id}.{field} refuses {new_value!r} — no "
                        f"matching row exists ({exc}). To clear the field instead, use "
                        "`gtd correct ... --null`.") from None
                raise
            _mark_moved(conn, record_kind, record_id)

        cur = conn.execute(
            """INSERT INTO correction
               (record_kind, record_id, field, old_value, new_value, reason, shape, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (record_kind, record_id, field,
             None if old is None else str(old),
             None if new_value is None else str(new_value),
             reason, f"{record_kind}:{field}", now()))
        correction_id = int(cur.lastrowid)
    journal.correction(record_kind, record_id, field, old, new_value, reason, record_text=label,
                       note=note)
    return correction_id


def retire_corrections(conn: sqlite3.Connection, record_kind: str, record_id: int) -> None:
    """A correction whose target is deleted is retired, not destroyed, because the pattern
    has to remain readable."""
    with tx(conn):
        conn.execute(
            "UPDATE correction SET retired=1 WHERE record_kind=? AND record_id=?",
            (record_kind, record_id))


def record_surfacing(conn: sqlite3.Connection, items: list[tuple[str, int]],
                     cycle: str, band: str | None = None) -> int:
    """Log that these records were put in front of the operator in this cycle.

    Idempotent per cycle, which is what keeps 'a missed review is not a decision' true: a
    run that never happened records nothing, so nothing advances.
    """
    n = 0
    with tx(conn):
        for kind, rid in items:
            cur = conn.execute(
                "INSERT OR IGNORE INTO surfacing_event (record_kind, record_id, band, cycle, created_at) "
                "VALUES (?,?,?,?,?)",
                (kind, rid, band, cycle, now()))
            n += cur.rowcount or 0
    return n


def record_withheld(conn: sqlite3.Connection, record_kind: str, record_id: int,
                    cycle: str, reason: str) -> None:
    """Every suppression is a decision the agent made and can defend. This is where it says
    so, before being asked."""
    with tx(conn):
        conn.execute(
            "INSERT INTO withheld_decision (record_kind, record_id, cycle, reason, created_at) "
            "VALUES (?,?,?,?,?)",
            (record_kind, record_id, cycle, reason, now()))


def record_rejection(conn: sqlite3.Connection, action_id: int | None, complaint: str,
                     raw_text: str = "", reviewers_passed: str = "") -> int:
    """Point of use is ground truth. When the operator rejects an action a reviewer passed, that is
    evidence the reviewer's brief is wrong, and it accumulates into its test cases."""
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO rejection (action_id, raw_text, complaint, reviewers_passed, created_at) "
            "VALUES (?,?,?,?,?)",
            (action_id, raw_text, complaint, reviewers_passed, now()))
        return int(cur.lastrowid)
