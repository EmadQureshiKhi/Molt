-- Policy state: the rules, the matches they produced, the approvals awaiting an
-- operator, and the position the watcher resumes from.
--
-- Three shapes here are load-bearing rather than incidental.
--
-- A rule's payload shape depends on its match kind. A cost threshold rule and a
-- path pattern rule do not carry the same fields, so the columns that could hold
-- either are all nullable and one check ties the populated set to the declared
-- kind. Without it an incoherent rule would store cleanly and then simply never
-- match, which is the worst outcome available: a rule an operator believes is
-- guarding something while it guards nothing. The check makes such a rule
-- unstorable instead.
--
-- The uniqueness constraints on the match and approval tables are the
-- deduplication that makes redelivery safe. The write stream redelivers
-- mutations after a watcher restart, and a redelivered mutation must not produce
-- a second halt or a second approval entry. These constraints are what guarantee
-- that, rather than any application-side check: a redelivered mutation collides
-- with the constraint and the insert resolves to a no-op. That is also what makes
-- the set of triggered actions independent of the order mutations are evaluated
-- in.
--
-- The watermark table is what the watcher resumes from, and it holds the
-- consumption mode alongside the position because the two are read together. The
-- sinkless change stream is the primary path on the delivered cluster and
-- polling is the retained fallback, so which one is in use is durable state
-- rather than a process-local fact.

CREATE TABLE IF NOT EXISTS policy_rule (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          STRING NOT NULL,
    enabled       BOOL NOT NULL DEFAULT true,
    match_kind    STRING NOT NULL,
    pattern       STRING NULL,
    client_id     UUID NULL REFERENCES client (id),
    threshold     FLOAT8 NULL,
    window_events INT NULL,
    action        STRING NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT rule_name_unique UNIQUE (name),
    CONSTRAINT rule_match_kind_known CHECK (match_kind IN (
        'file_path', 'shell_command', 'client', 'session_cost', 'error_rate')),
    CONSTRAINT rule_action_known CHECK (action IN (
        'allow', 'warn', 'require_approval', 'halt_agent')),
    -- The shape-validity check: the fields a rule carries must be the fields its
    -- match kind is evaluated against.
    CONSTRAINT rule_shape_valid CHECK (
        (match_kind IN ('file_path', 'shell_command') AND pattern IS NOT NULL)
        OR (match_kind = 'client' AND client_id IS NOT NULL)
        OR (match_kind IN ('session_cost', 'error_rate') AND threshold IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS policy_match (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id    UUID NOT NULL REFERENCES policy_rule (id),
    session_id UUID NOT NULL,
    event_id   UUID NULL,
    action     STRING NOT NULL,
    detail     JSONB NOT NULL DEFAULT '{}'::JSONB,
    matched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One match per rule, session, and triggering mutation. A redelivered
    -- mutation collides here and resolves to a no-op rather than a second halt.
    CONSTRAINT match_unique UNIQUE (rule_id, session_id, event_id),
    INDEX match_by_session (session_id, matched_at DESC)
);

CREATE TABLE IF NOT EXISTS approval_queue (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id      UUID NOT NULL REFERENCES policy_rule (id),
    session_id   UUID NOT NULL,
    event_id     UUID NULL,
    status       STRING NOT NULL DEFAULT 'pending',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- The resolving principal, the decision, and the resolution instant are
    -- written when an operator resolves the entry, and only then.
    resolved_by  STRING NULL,
    decision     STRING NULL,
    resolved_at  TIMESTAMPTZ NULL,
    CONSTRAINT approval_status_known CHECK (status IN ('pending', 'resolved')),
    CONSTRAINT approval_decision_known CHECK (
        decision IS NULL OR decision IN ('approved', 'denied')),
    -- The same deduplication, for the same reason: a redelivered mutation must
    -- not enqueue a second approval for one rule, session, and mutation.
    CONSTRAINT approval_unique UNIQUE (rule_id, session_id, event_id),
    -- Serves the operator's list of unresolved entries, and the blocking check
    -- the capture side performs while an entry for a session is pending.
    INDEX approval_pending (session_id) WHERE status = 'pending'
);

CREATE TABLE IF NOT EXISTS watcher_watermark (
    id                UUID PRIMARY KEY,
    mode              STRING NOT NULL,
    last_mutation_at  TIMESTAMPTZ NULL,
    last_event_id     UUID NULL,
    resolved_at       TIMESTAMPTZ NULL,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT watermark_mode_known CHECK (mode IN ('changefeed', 'polling'))
);
