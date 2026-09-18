"""Versioned schema migrations.

Two shapes in one database. The bulk is ordinary fixed-column tables — a record whose
fields have a single author is a row. The entity layer is layered attribute rows, because
it is the one place three provenances compete for the same field and precedence has to be
composed on read.

Two things the naive schema would store and this one does not, both because storing them
is where drift starts: next-ness is a query and never a column, so nothing can write a
`next` status; and blocking is a typed predicate on the blocked action, never a maintained
forward graph.
"""
from __future__ import annotations

import sqlite3

MIGRATIONS: list[tuple[int, str]] = []


def _m(version: int, sql: str) -> None:
    MIGRATIONS.append((version, sql))


_m(1, """
CREATE TABLE area (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    standard        TEXT,
    health_note     TEXT,
    vault_path      TEXT,
    state           TEXT NOT NULL DEFAULT 'active'
                    CHECK (state IN ('active','dormant','archived')),
    created_at      TEXT NOT NULL
);

CREATE TABLE project (
    id              INTEGER PRIMARY KEY,
    outcome         TEXT NOT NULL,
    done_test       TEXT,
    area_id         INTEGER REFERENCES area(id),
    state           TEXT NOT NULL DEFAULT 'active'
                    CHECK (state IN ('active','someday','on-hold','done','dropped','demoted')),
    vault_path      TEXT,
    created_at      TEXT NOT NULL,
    closed_at       TEXT,
    done_test_checked_at TEXT,
    legacy_id       TEXT
);
CREATE INDEX ix_project_state ON project(state);

-- The atomic, non-fungible record. One row, one author, appearing in many views rather
-- than duplicated across lists. There is deliberately no `next` column and no priority,
-- importance, duration or energy field.
CREATE TABLE action (
    id              INTEGER PRIMARY KEY,
    text            TEXT NOT NULL,
    project_id      INTEGER REFERENCES project(id),
    area_id         INTEGER REFERENCES area(id),

    -- Four closed-vocabulary context dimensions, at most one value each.
    commitment_type TEXT CHECK (commitment_type IN ('obligation','intention')),
    work_type       TEXT CHECK (work_type IN ('open','closed')),
    execution_type  TEXT CHECK (execution_type IN ('interactive','automated')),
    person_id       INTEGER REFERENCES entity(id),

    -- Two time fields, never conflated. hard_date is a date-and-time-specific commitment
    -- and stays scarce enough to be believed; plan_date is soft intent.
    hard_date       TEXT,
    plan_date       TEXT,

    commit_week     TEXT,

    state           TEXT NOT NULL DEFAULT 'open'
                    CHECK (state IN ('open','done','dropped','someday')),
    created_at      TEXT NOT NULL,
    done_at         TEXT,
    closed_at       TEXT,

    source_kind     TEXT,
    source_path     TEXT,
    source_line     INTEGER,
    inbox_item_id   INTEGER REFERENCES inbox_item(id),
    legacy_id       TEXT
);
CREATE INDEX ix_action_state ON action(state);
CREATE INDEX ix_action_project ON action(project_id);
CREATE INDEX ix_action_commit ON action(commit_week);
CREATE INDEX ix_action_legacy ON action(legacy_id);

-- A block is a single backward pointer on the blocked action. An action may hold a small
-- flat set composed with AND — no nesting, no OR — which keeps evaluation trivial while
-- covering the real cases. A wrong pointer corrupts only its own action.
CREATE TABLE block (
    id              INTEGER PRIMARY KEY,
    action_id       INTEGER NOT NULL REFERENCES action(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CHECK (kind IN
                    ('done','date_reached','exists','confirmed','proximity','judgment')),
    ref_kind        TEXT CHECK (ref_kind IN ('action','project','waiting_for')),
    ref_id          INTEGER,
    ref_date        TEXT,
    ref_path        TEXT,
    ref_entity_id   INTEGER REFERENCES entity(id),
    prose           TEXT,
    satisfied       INTEGER NOT NULL DEFAULT 0,
    satisfied_at    TEXT,
    last_evaluated  TEXT,
    -- Set when the operator flips a block by hand ("I'm about to call a person"). Mechanical kinds are
    -- recomputed on every refresh; a manual override is sticky and survives recomputation.
    manual_override INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);
CREATE INDEX ix_block_action ON block(action_id, satisfied);
CREATE INDEX ix_block_ref ON block(kind, ref_kind, ref_id, satisfied);

-- A first-class named state, not something reconstructed by evaluating predicates: it is
-- the one suppressed set the operator actively wants to scan on demand.
CREATE TABLE waiting_for (
    id              INTEGER PRIMARY KEY,
    counterparty    TEXT NOT NULL,
    counterparty_id INTEGER REFERENCES entity(id),
    expectation     TEXT NOT NULL,
    expected_by     TEXT,
    project_id      INTEGER REFERENCES project(id),
    is_agent        INTEGER NOT NULL DEFAULT 0,
    state           TEXT NOT NULL DEFAULT 'open'
                    CHECK (state IN ('open','delivered','dropped','converted')),
    chase_count     INTEGER NOT NULL DEFAULT 0,
    ripe_cycles     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    closed_at       TEXT,
    source_path     TEXT,
    legacy_id       TEXT
);
CREATE INDEX ix_waiting_state ON waiting_for(state);

-- Raw, verbatim, provenance mandatory. Archived on clarify, never destroyed: this is the
-- bedrock the catastrophic rebuild replays.
CREATE TABLE inbox_item (
    id              INTEGER PRIMARY KEY,
    raw             TEXT NOT NULL,
    provenance      TEXT NOT NULL,
    captured_at     TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'captured'
                    CHECK (state IN ('captured','clarify_ready','clarified','reference','discarded')),
    draft           TEXT,
    qc              TEXT,
    self_uncertainty TEXT,
    reason          TEXT,
    clarified_at    TEXT,
    result_kind     TEXT,
    result_id       INTEGER
);
CREATE INDEX ix_inbox_state ON inbox_item(state);

CREATE TABLE reference (
    id              INTEGER PRIMARY KEY,
    path            TEXT NOT NULL,
    why             TEXT,
    relates_kind    TEXT,
    relates_id      INTEGER,
    inbox_item_id   INTEGER REFERENCES inbox_item(id),
    created_at      TEXT NOT NULL
);

-- Staleness is measured in surfacing cycles, never calendar time. An action never
-- surfaced cannot be stale by neglect — it is suppressed, and suppression is the weekly's
-- business rather than the daily's.
CREATE TABLE surfacing_event (
    id              INTEGER PRIMARY KEY,
    record_kind     TEXT NOT NULL,
    record_id       INTEGER NOT NULL,
    band            TEXT,
    cycle           TEXT NOT NULL,
    moved           INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);
CREATE INDEX ix_surfacing_record ON surfacing_event(record_kind, record_id, created_at);
CREATE UNIQUE INDEX ux_surfacing_cycle ON surfacing_event(record_kind, record_id, cycle);

-- What the agent chose *not* to surface, and why. Distinct from the mechanically blocked
-- set: these are selection decisions, and they are what makes band two auditable.
CREATE TABLE withheld_decision (
    id              INTEGER PRIMARY KEY,
    record_kind     TEXT NOT NULL,
    record_id       INTEGER NOT NULL,
    cycle           TEXT NOT NULL,
    reason          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX ix_withheld_cycle ON withheld_decision(cycle);

-- Every asserted override, keyed to the record and field it corrects. Cross-cutting: for
-- the entity layer it doubles as the asserted provenance layer the read composes;
-- everywhere else it is the stream drift detection reads for the same-shape-recurring
-- signal. A correction whose target is deleted is retired, not destroyed.
CREATE TABLE correction (
    id              INTEGER PRIMARY KEY,
    record_kind     TEXT NOT NULL,
    record_id       INTEGER NOT NULL,
    field           TEXT NOT NULL,
    old_value       TEXT,
    new_value       TEXT,
    reason          TEXT,
    shape           TEXT NOT NULL,
    retired         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);
CREATE INDEX ix_correction_shape ON correction(shape, created_at);
CREATE INDEX ix_correction_record ON correction(record_kind, record_id);

-- Point-of-use rejections: when the operator flags a surfaced action as badly formed, clarify did
-- not finish its job. These accumulate into the reviewers' test cases.
CREATE TABLE rejection (
    id              INTEGER PRIMARY KEY,
    action_id       INTEGER REFERENCES action(id),
    raw_text        TEXT,
    complaint       TEXT,
    reviewers_passed TEXT,
    created_at      TEXT NOT NULL
);

-- Entity layer: identity plus a stack of attribute rows. Identity binds to the calendar
-- recurrence series, never to a mutable attribute.
CREATE TABLE entity (
    id              INTEGER PRIMARY KEY,
    type            TEXT NOT NULL CHECK (type IN ('meeting','person')),
    identity_key    TEXT NOT NULL,
    merged_into     INTEGER REFERENCES entity(id),
    vault_path      TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE(type, identity_key)
);

CREATE TABLE entity_attribute (
    id              INTEGER PRIMARY KEY,
    entity_id       INTEGER NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    field           TEXT NOT NULL,
    value           TEXT,
    provenance      TEXT NOT NULL CHECK (provenance IN ('source','inferred','asserted')),
    occurrence_key  TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL,
    UNIQUE(entity_id, field, provenance, occurrence_key)
);
CREATE INDEX ix_entattr ON entity_attribute(entity_id, field);

-- Generation rules: cadence- and date-driven fire mechanically in `gtd generate`;
-- condition-driven are left for the morning run's judgment. This is where the vault's
-- long-deferred recurrence engine lands.
CREATE TABLE generation_rule (
    id              INTEGER PRIMARY KEY,
    owner_kind      TEXT NOT NULL CHECK (owner_kind IN ('area','project')),
    owner_id        INTEGER NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('cadence','date','condition')),
    expr            TEXT NOT NULL,
    action_text     TEXT NOT NULL,
    commitment_type TEXT,
    work_type       TEXT,
    execution_type  TEXT,
    anchor_date     TEXT,
    last_generated  TEXT,
    state           TEXT NOT NULL DEFAULT 'active'
                    CHECK (state IN ('active','retired')),
    legacy_id       TEXT,
    source_path     TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX ix_genrule_owner ON generation_rule(owner_kind, owner_id);

-- The projection's authoritative baseline: the exact bytes the generator last wrote
-- between a block's markers. The block-diff has a baseline rather than a guess.
CREATE TABLE block_snapshot (
    id              INTEGER PRIMARY KEY,
    path            TEXT NOT NULL,
    block_id        TEXT NOT NULL,
    content         TEXT NOT NULL,
    sha             TEXT NOT NULL,
    written_at      TEXT NOT NULL,
    UNIQUE(path, block_id)
);

-- The drain's ledger. Ingestion is tracked store-side because the drain never writes the
-- Inbox note: a VPS rewrite racing a mobile append across Obsidian Sync would fork the
-- capture surface itself.
CREATE TABLE ingest_ledger (
    id              INTEGER PRIMARY KEY,
    line_hash       TEXT NOT NULL UNIQUE,
    source          TEXT NOT NULL,
    first_seen      TEXT NOT NULL,
    inbox_item_id   INTEGER REFERENCES inbox_item(id)
);

-- The one-shot migration's ledger, keyed on legacy [id::] where present and source-line
-- hash where not, so the import is idempotent and can be iterated to convergence.
CREATE TABLE import_ledger (
    id              INTEGER PRIMARY KEY,
    key             TEXT NOT NULL UNIQUE,
    disposition     TEXT NOT NULL,
    reason          TEXT,
    record_kind     TEXT,
    record_id       INTEGER,
    source_path     TEXT,
    source_line     INTEGER,
    raw             TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX ix_import_disp ON import_ledger(disposition);
""")


# --- 2 --------------------------------------------------------------------------------------
# Identity is permanent, because everything else points at it by number.
#
# `INTEGER PRIMARY KEY` is a rowid alias, so SQLite assigns max(id)+1 — delete the highest row
# and the next insert reuses its number. Nothing would error; the journal's `action:183`, every
# correction's record_id, every block ref and surfacing event would silently re-point at a
# different task. That is the exact class of failure this store exists to remove.
#
# The alternative was AUTOINCREMENT, which needs a full table rebuild — and with foreign keys
# enforced, rebuilding five referenced tables on the system of record is the riskier change.
# It also treats the symptom. The only DELETE anywhere in the code is on `block`; nothing
# deletes an identity-bearing row, because `done`, `drop` and `park` are state changes that
# keep it ("archiving is a move, not a loss"). The invariant already held — what broke it on
# 2026-07-26 was hand-written SQL reaching around the CLI to clean up mis-keyed entities.
#
# So the guard makes the reuse *condition* impossible rather than the mechanism, and it fires
# on exactly the path that failed. Maintenance that genuinely must delete has to drop the
# trigger first, which is a deliberate act and leaves a trace.
_m(2, """
CREATE TRIGGER action_is_never_deleted BEFORE DELETE ON action
BEGIN
    SELECT RAISE(ABORT, 'actions are never deleted: done/drop/park keep the row, and deleting the highest id would let the next insert reuse its number');
END;

CREATE TRIGGER project_is_never_deleted BEFORE DELETE ON project
BEGIN
    SELECT RAISE(ABORT, 'projects are never deleted: project-close keeps the record whole');
END;

CREATE TRIGGER inbox_item_is_never_deleted BEFORE DELETE ON inbox_item
BEGIN
    SELECT RAISE(ABORT, 'raw captures are archived on clarification, never destroyed');
END;

CREATE TRIGGER waiting_for_is_never_deleted BEFORE DELETE ON waiting_for
BEGIN
    SELECT RAISE(ABORT, 'waiting-fors are closed, never deleted');
END;

CREATE TRIGGER entity_is_never_deleted BEFORE DELETE ON entity
BEGIN
    SELECT RAISE(ABORT, 'entities merge via merged_into, never by deletion');
END;
""")


# An agent is not a person. The import had only 'person' to put an agent in, and the
# consequences were not cosmetic: a proximity block naming it passed the names-a-person test
# and then could never come true, because proximity means "the operator is about to see them" and
# there is no such moment for a tool on his Mac. One real piece of work sat hidden behind
# exactly that. Giving agents their own type lets the store refuse the mistake at the point of
# entry rather than describe it afterwards.
#
# Rebuilding `entity` is the one thing in this schema that cannot happen with foreign keys
# live. `DROP TABLE` performs an implicit DELETE when they are on, and `entity_attribute`
# cascades from entity(id) — so the rebuild would have silently emptied every composed
# attribute in the store while looking like it worked. `defer_foreign_keys` does not help:
# it moves the *check* to COMMIT, not the cascade. So this migration is declared in
# NEEDS_FOREIGN_KEYS_OFF and the runner follows SQLite's documented rebuild procedure —
# keys off outside the transaction, `foreign_key_check` afterwards, keys back on.
_m(3, """
DROP TRIGGER entity_is_never_deleted;

CREATE TABLE entity_new (
    id              INTEGER PRIMARY KEY,
    type            TEXT NOT NULL CHECK (type IN ('meeting','person','agent')),
    identity_key    TEXT NOT NULL,
    merged_into     INTEGER REFERENCES entity(id),
    vault_path      TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE(type, identity_key)
);

INSERT INTO entity_new (id, type, identity_key, merged_into, vault_path, created_at)
SELECT id, type, identity_key, merged_into, vault_path, created_at FROM entity;

DROP TABLE entity;
ALTER TABLE entity_new RENAME TO entity;

CREATE TRIGGER entity_is_never_deleted BEFORE DELETE ON entity
BEGIN
    SELECT RAISE(ABORT, 'entities merge via merged_into, never by deletion');
END;

UPDATE entity SET type='agent'
 WHERE type='person'
   AND identity_key IN ('agent-runtime','claude','codex','opencode','hermes',
                        'claude code','claude desktop','codex cli','the agent','agent');

-- A block that waits for the operator to be near an agent can never come true, so it is not a
-- suppression: it is a record disappearing. Drop those; the work returns to his lists.
DELETE FROM block
 WHERE kind IN ('proximity','confirmed')
   AND ref_entity_id IN (SELECT id FROM entity WHERE type='agent');
""")


# Agent work needs somewhere to say how it runs and what it is, or the handoff cannot be
# dispatched without a human retyping it. `expectation` is a one-line summary written for
# the operator to read in a review; it is not a brief an unattended run can execute from.
#
# `lane` is enforced in Python against config.LANES rather than by a CHECK, because adding a
# checked column means rebuilding the table, and migration 3 is the cautionary tale for how
# much that costs on this schema.
_m(4, """
ALTER TABLE waiting_for ADD COLUMN lane TEXT;
ALTER TABLE waiting_for ADD COLUMN brief TEXT;
ALTER TABLE waiting_for ADD COLUMN dispatched_at TEXT;
ALTER TABLE waiting_for ADD COLUMN dispatch_path TEXT;
""")


# A recurring obligation owned by an area spawned with no project, so the work that feeds it
# — clarified separately, into the project, undated — could not be seen next to it. On
# 2026-07-28 the morning surface proposed posting the weekly data update to Slack, due that
# day, while the three unfinished pieces it depends on stayed invisible: the dated obligation
# was in an area, the work was in a project, and nothing joined them.
#
# The rule's owner and the spawned action's project are different questions. The Work area
# owns the recurrence; the Weekly data update project owns the work. This column lets a rule
# say so, without moving the rule out of the area whose standard it maintains.
_m(5, """
ALTER TABLE generation_rule ADD COLUMN project_id INTEGER REFERENCES project(id);
""")


# A drop's reason previously lived only in the append-only journal — plain text, "never
# rewritten" by that file's own rule. A misquoted reason (the operator's own words attached to a
# complaint he never made) then had nowhere to be corrected: the disposition ('dropped')
# was right, but the record's account of why was permanently wrong. Storing it on the row
# gives it a field `gtd correct` can amend without touching `state`.
_m(6, """
ALTER TABLE action ADD COLUMN reason TEXT;
""")


# Transport idempotency is durable owner state in the canonical database, not a second GTD
# store. The canonical inbox row and this receipt are committed together, so a response lost
# after COMMIT can be reconciled without performing the capture again. Keys do not expire in
# version 1. The persisted response excludes request-local trace fields; a replay binds it to
# the new request id while preserving the original canonical evidence.
_m(7, """
CREATE TABLE transport_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    operation       TEXT NOT NULL CHECK (operation = 'inbox.capture'),
    payload_digest  TEXT NOT NULL,
    canonical_id    INTEGER NOT NULL REFERENCES inbox_item(id),
    canonical_version TEXT NOT NULL,
    response_json   TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    original_request_id TEXT NOT NULL,
    surface_id      TEXT NOT NULL,
    principal       TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX ix_transport_canonical ON transport_idempotency(operation, canonical_id);
""")


# Same gap as migration 6, one record type over: `waiting-close` had no way to say why a
# waiting-for was closed, so a dropped wait — exactly the record someone later asks "why did
# this go away?" about — carried its reason only in conversation. Mirrors action.reason.
_m(8, """
ALTER TABLE waiting_for ADD COLUMN reason TEXT;
""")


# The commitment-mutation half of the transport ledger, deliberately a second table rather
# than a widened `transport_idempotency`. That table's CHECK pins it to `inbox.capture` and its
# `canonical_id` is a foreign key into `inbox_item`; a commitment mutation's canonical identity
# is not always one inbox row — a surfacing receipt names a set of records, a daily render names
# a day and its blocks — so it is stored as a canonical reference document instead. Keeping the
# capture ledger byte-identical is also what makes the version-1 capture path provably unchanged
# rather than merely believed to be.
#
# A key is unique across *both* ledgers: the gateway refuses a key already spent on the other
# kind of effect rather than letting one idempotency key mean two different things.
_m(9, """
CREATE TABLE transport_mutation_receipt (
    idempotency_key TEXT PRIMARY KEY,
    operation       TEXT NOT NULL,
    payload_digest  TEXT NOT NULL,
    canonical_ref   TEXT NOT NULL,
    canonical_version TEXT NOT NULL,
    response_json   TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    authorization_digest TEXT NOT NULL,
    original_request_id TEXT NOT NULL,
    surface_id      TEXT NOT NULL,
    principal       TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX ix_transport_mutation_operation
    ON transport_mutation_receipt(operation, created_at);
""")


# A refused landing left `dispatched_at` stamped forever, whatever the refusal was — the
# column had a writer (dispatch) and a reader (dispatch's own selection) and no eraser.
# AGENTWORK-277 (a Cloudflare 521 mid-run) and the advance-recurring job (worktree unusable,
# twice, un-noticed for 23 days) are the same defect: a stuck dispatch is invisible both to
# `gtd dispatch`, which will never pick it up again, and to the operator, because it looks identical
# to work still in flight.
#
# This table is the audit trail for every time that stamp is cleared other than by the
# ordinary re-dispatch it started from: an automatic re-arm on a positively-identified
# infrastructure failure (`kind='auto'`, capped — see `dispatch.reap_retryable`), or a
# human's own named decision to try again (`kind='manual'`, uncapped — `gtd dispatch --retry`,
# the sanctioned replacement for correcting `dispatched_at` by hand). `reason` is required for
# both: an automatic row carries the refusal text that triggered it, a manual one carries
# whatever the person running the command gave `--reason`. `source_at` pins an automatic row
# to the exact landing record that triggered it, which is what makes re-scanning the landing
# log idempotent — once a waiting-for is re-stamped, its `dispatched_at` moves past that
# timestamp and the same refusal is never actioned twice.
_m(10, """
CREATE TABLE dispatch_retry (
    id              INTEGER PRIMARY KEY,
    waiting_for_id  INTEGER NOT NULL REFERENCES waiting_for(id),
    kind            TEXT NOT NULL CHECK (kind IN ('auto', 'manual')),
    reason          TEXT NOT NULL,
    source_at       TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX ix_dispatch_retry_waiting ON dispatch_retry(waiting_for_id, created_at);
""")


# Migrations that rebuild a table other rows point at. See the note above migration 3: with
# foreign keys live, DROP TABLE runs an implicit DELETE that fires ON DELETE CASCADE on the
# children, so a rebuild destroys data while appearing to succeed. Keys go off around these,
# and `foreign_key_check` has to come back clean before they go on again.
NEEDS_FOREIGN_KEYS_OFF = frozenset({3})


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Returns the resulting schema version."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    current = row["v"] or 0
    for version, sql in sorted(MIGRATIONS):
        if version <= current:
            continue
        rebuild = version in NEEDS_FOREIGN_KEYS_OFF
        if rebuild:
            # Must be outside a transaction: this pragma is a silent no-op inside one.
            conn.execute("PRAGMA foreign_keys=OFF")
        try:
            # BEGIN/COMMIT live inside the script: executescript() issues an implicit COMMIT
            # for any pending transaction, so an outer BEGIN here would be silently discarded.
            conn.executescript(
                "BEGIN;\n"
                + sql
                + f"\nINSERT INTO schema_version (version) VALUES ({int(version)});\nCOMMIT;\n"
            )
            if rebuild:
                broken = list(conn.execute("PRAGMA foreign_key_check"))
                if broken:
                    raise sqlite3.IntegrityError(
                        f"migration {version} left {len(broken)} dangling reference(s): "
                        f"{broken[:5]}")
        finally:
            if rebuild:
                conn.execute("PRAGMA foreign_keys=ON")
        current = version
    return current


def schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return row["v"] or 0
    except sqlite3.OperationalError:
        return 0
