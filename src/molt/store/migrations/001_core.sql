-- Core schema: the migration history, tenants, sessions, and the event ledger.
--
-- The ledger is the append-only system of record. Three shapes here are
-- load-bearing rather than incidental.
--
-- The ledger carries its own tenant identifier, non-null, from this first
-- migration. It is denormalised from the session row on purpose: the explicit
-- erasure sweep, the per-tenant index, and the tenancy filter on every read all
-- need it without a join, and adding it later would leave a window in which the
-- ledger could not answer whose data a row holds.
--
-- Sequence and chain uniqueness are constraints rather than conventions. One
-- sequence number per session and one successor per predecessor digest together
-- make the per-session chain a line rather than a tree, so a retrospective
-- insertion cannot hide inside a fork.
--
-- The session-to-event reference is added at the end, after both tables exist,
-- because a session may name the event that spawned it while every event names
-- its session. Neither table can be created after the other, so the reference is
-- deferred to an alteration that follows both.

CREATE TABLE IF NOT EXISTS schema_migration (
    version     INT PRIMARY KEY,
    name        STRING NOT NULL,
    file_digest STRING NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS client (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug               STRING NOT NULL,
    display_name       STRING NOT NULL,
    jurisdiction       STRING NOT NULL DEFAULT 'default',
    retention_interval INTERVAL NOT NULL DEFAULT INTERVAL '90 days',
    content_markers    STRING[] NOT NULL DEFAULT ARRAY[]::STRING[],
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT client_slug_unique UNIQUE (slug),
    CONSTRAINT client_retention_positive CHECK (retention_interval > INTERVAL '0')
);

-- The reserved tenant every session falls back to when the workspace mapping
-- names none. The identifier is fixed rather than generated so that the capture
-- side can name it without a lookup, and the insert resolves a conflict so a
-- second run changes nothing.
INSERT INTO client (id, slug, display_name, jurisdiction)
VALUES ('00000000-0000-4000-8000-000000000000', 'unassigned', 'Unassigned workspace', 'default')
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS session (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id           UUID NOT NULL REFERENCES client (id),
    agent_cli           STRING NOT NULL,
    machine_id          STRING NOT NULL,
    team_id             STRING NULL,
    attribution         JSONB NOT NULL DEFAULT '{}'::JSONB,
    workspace_path      STRING NULL,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at            TIMESTAMPTZ NULL,
    outcome             STRING NOT NULL DEFAULT 'in_progress',
    parent_session_id   UUID NULL REFERENCES session (id),
    spawning_event_id   UUID NULL,
    depth               INT NOT NULL DEFAULT 0,
    tool_call_count     INT NOT NULL DEFAULT 0,
    model_request_count INT NOT NULL DEFAULT 0,
    error_count         INT NOT NULL DEFAULT 0,
    token_count         INT8 NOT NULL DEFAULT 0,
    cost_usd            DECIMAL(14, 6) NOT NULL DEFAULT 0,
    halted              BOOL NOT NULL DEFAULT false,
    halted_at           TIMESTAMPTZ NULL,
    halt_reason         STRING NULL,
    halt_rule_id        UUID NULL,
    CONSTRAINT session_outcome_known CHECK (
        outcome IN ('in_progress', 'succeeded', 'failed', 'abandoned')),
    CONSTRAINT session_depth_non_negative CHECK (depth >= 0),
    -- Half of the nesting invariant: a session with no parent sits at depth
    -- zero. The parent-plus-one half is enforced by the inserting statement,
    -- which reads the parent's depth rather than trusting the caller.
    CONSTRAINT session_root_depth CHECK (
        (parent_session_id IS NULL AND depth = 0) OR parent_session_id IS NOT NULL),
    INDEX session_by_client (client_id, started_at DESC),
    INDEX session_by_parent (parent_session_id),
    INDEX session_by_machine (machine_id, started_at DESC),
    INDEX session_halted (halted) WHERE halted
);

CREATE TABLE IF NOT EXISTS ledger (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id        UUID NOT NULL REFERENCES session (id),
    client_id         UUID NOT NULL REFERENCES client (id),
    seq               INT NOT NULL,
    category          STRING NOT NULL,
    occurred_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent_cli         STRING NOT NULL,
    machine_id        STRING NOT NULL,
    -- A result names the call it answers through a column rather than by being
    -- nested inside that call's payload, so the relationship is queryable.
    parent_event_id   UUID NULL REFERENCES ledger (id),
    payload           JSONB NOT NULL,
    redacted          BOOL NOT NULL DEFAULT false,
    text_body         STRING NULL,
    content_digest    STRING NOT NULL,
    prev_chain_digest STRING NOT NULL,
    chain_digest      STRING NOT NULL,
    embedding_state   STRING NOT NULL DEFAULT 'not_required',
    expires_at        TIMESTAMPTZ NOT NULL,
    CONSTRAINT ledger_category_known CHECK (category IN (
        'session_start', 'session_end', 'user_prompt', 'assistant_response',
        'tool_call', 'tool_result', 'model_request', 'model_response',
        'file_read', 'file_write', 'shell_command', 'decision', 'error',
        'cost_record', 'recall', 'policy_halt')),
    CONSTRAINT ledger_embedding_state_known CHECK (embedding_state IN (
        'not_required', 'pending', 'embedded', 'failed')),
    CONSTRAINT ledger_seq_positive CHECK (seq > 0),
    CONSTRAINT ledger_digest_hex CHECK (
        length(content_digest) = 64 AND length(chain_digest) = 64
        AND length(prev_chain_digest) = 64),
    CONSTRAINT ledger_seq_unique_in_session UNIQUE (session_id, seq),
    CONSTRAINT ledger_one_successor_per_predecessor UNIQUE (session_id, prev_chain_digest),
    INDEX ledger_by_session_seq (session_id, seq ASC),
    INDEX ledger_by_client_time (client_id, occurred_at DESC),
    -- Serves the watermark scan of the write-stream polling fallback.
    INDEX ledger_by_recorded (recorded_at ASC, id ASC),
    INDEX ledger_pending_embedding (recorded_at ASC) WHERE embedding_state = 'pending',
    INDEX ledger_by_parent (parent_event_id)
);

ALTER TABLE session ADD CONSTRAINT session_spawning_event_fk
    FOREIGN KEY (spawning_event_id) REFERENCES ledger (id);

-- A reference added by an alteration is recorded as unvalidated until the rows
-- already present have been checked. There are none at this point, so the check
-- is immediate, and validating here is what makes the reference introspect as an
-- ordinary foreign key rather than as one carrying an unfinished check.
ALTER TABLE session VALIDATE CONSTRAINT session_spawning_event_fk;

