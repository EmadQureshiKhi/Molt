-- Confidence-weighted procedural memory: a learned procedure earns and loses
-- standing from how the sessions that used it ended.
--
-- This is the migration that makes the procedural tier improve with use rather
-- than only accumulate. Before it, a learned procedure was a body of text that
-- recall could return forever regardless of whether following it ever helped.
-- After it, the same procedure carries a confidence value, every retrieval and
-- every session outcome that touched it is on the record, and every movement of
-- the value is accompanied by a change record naming what it moved from, what it
-- moved to, and which outcome caused the movement. A claim that memory got better
-- is then a query result rather than an assertion.
--
-- Five shapes here are load-bearing rather than incidental.
--
-- The confidence column and the artifact kind stand in an equivalence, not an
-- implication. A summary can never acquire a confidence value and a learned
-- procedure can never be stored without one. The weaker shape -- a range check on
-- a nullable column, with the pairing left to the writing code -- was rejected
-- because both of its failure modes are silent. A summary carrying a stray
-- confidence would sort into a recall tie-break it has no business in, and a
-- learned procedure with no value would be excluded by the floor predicate and
-- so disappear from recall while remaining stored, which reads as data loss and
-- is not. The equivalence makes both shapes unwritable by any role. The same rule
-- is asserted a second time in the model layer, deliberately: the database is
-- what makes it true of stored rows and the model is what makes the violation
-- reportable before a round trip.
--
-- One partial index serves both uses recall makes of the confidence value.
-- Recall orders by ascending cosine distance and breaks a tie by descending
-- confidence, and separately excludes any procedure sitting below the configured
-- floor. Those look like two access patterns and are one: an index over the
-- confidence value in descending order, restricted to the procedure kind, gives
-- the tie-break its ordering directly and gives the floor a bounded range scan
-- from the top down to the floor value. A second index would double the write
-- cost of every confidence movement to serve a predicate the first one already
-- covers. The predicate names the kind rather than filtering on it at read time,
-- which is what keeps the index small: it holds procedure rows only, while the
-- artifact table holds every derived kind.
--
-- One session moves one procedure's confidence at most once. The uniqueness of
-- the procedure and session pair on the outcome table is what enforces it. A
-- session reporting its outcome twice -- a retry, a duplicated delivery, an
-- operator re-running a step -- would otherwise apply the adjustment twice and
-- the value would then reflect how many times the report arrived rather than how
-- the work went. The second report is refused by the database, so the adjustment
-- is idempotent per session without the recording path holding any state.
--
-- A change record asserts a change that actually happened. The check refusing
-- equal prior and new values is what makes the count of change records equal the
-- count of events that moved the value, which is the property the audited history
-- rests on. Two cases produce no movement and therefore no record: an abandoned
-- outcome, which adjusts nothing by design, and an adjustment applied to a value
-- already at a bound, where clamping absorbs it entirely. Writing a record for
-- either would put a movement in the history that never occurred, and a history
-- that overstates is no more usable than one that omits.
--
-- Deletion follows recomputability. A retrieval, an outcome, and a change record
-- are all statements about one procedure and describe nothing once that procedure
-- is gone, so each reference cascades. That is the opposite of the treatment
-- erasure evidence gets, and the difference is the point: a disposition is
-- evidence about a governed operation and survives its subject, while these three
-- are usage history about content and go with it. The references are declared
-- inline and carry platform-generated names, so a later migration that revisits
-- referential actions has names to replace.
--
-- Two notes on shape, both forced by the platform rather than chosen.
--
-- A partial index whose stored or indexed column was added earlier in the same
-- transaction is refused, because that column is not yet visible to the index
-- builder. A constraint cannot be removed and re-added under one name inside one
-- transaction either, for the same reason. The column addition therefore stays in
-- the migration's own transaction and every constraint statement and the index
-- creation carry the marker asking for a transaction of their own, which the
-- runner applies after the body has committed. The drop-then-add pairing is what
-- keeps the file re-runnable, since a constraint addition admits no guard of its
-- own.
--
-- The session identifier on the retrieval and outcome tables carries no
-- reference, for the reason a disposition's artifact identifier carries none: the
-- usage record must survive an erasure that removed the session, and a reference
-- would either block that erasure or vanish with it.
--
-- No privilege is granted here. The grants for every table this generation adds
-- are carried by the roles migration of this generation, so that the privilege
-- surface is stated in one place rather than accumulated across the files that
-- happen to create tables. The column-scoped update the confidence value needs
-- belongs there too.

ALTER TABLE derived_artifact ADD COLUMN IF NOT EXISTS procedure_confidence FLOAT8 NULL;

-- Every retrieval of a learned procedure, so the console can report how much a
-- procedure is actually used alongside how well it has done.
CREATE TABLE IF NOT EXISTS procedure_retrieval (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    procedure_id UUID NOT NULL REFERENCES derived_artifact (id) ON DELETE CASCADE,
    -- No reference: the record outlives an erasure that removed the session.
    session_id   UUID NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One procedure's retrievals newest first, which is the per-procedure count
    -- and the recent-use view the console renders.
    INDEX retrieval_by_procedure (procedure_id, retrieved_at DESC),
    -- Which procedures one session consumed, which is what the outcome path
    -- reads when a session reaches a terminal state.
    INDEX retrieval_by_session (session_id)
);

-- How the sessions that consumed a procedure ended. This is the only input that
-- moves a confidence value.
CREATE TABLE IF NOT EXISTS procedure_outcome (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    procedure_id UUID NOT NULL REFERENCES derived_artifact (id) ON DELETE CASCADE,
    -- No reference, for the same reason as on the retrieval table.
    session_id   UUID NOT NULL,
    outcome      STRING NOT NULL,
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- The three terminal classifications only. A session in flight has reached
    -- no outcome, so there is nothing to report and nothing to adjust.
    CONSTRAINT outcome_known CHECK (outcome IN ('succeeded', 'failed', 'abandoned')),
    -- One session contributes at most one outcome per procedure, so a duplicated
    -- report is refused rather than applied twice.
    CONSTRAINT outcome_unique_per_session UNIQUE (procedure_id, session_id),
    -- The per-classification counts the console shows, served without a fetch.
    INDEX outcome_by_procedure (procedure_id, outcome)
);

-- The audited history of confidence movement. One row per movement that happened.
CREATE TABLE IF NOT EXISTS procedure_confidence_change (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    procedure_id UUID NOT NULL REFERENCES derived_artifact (id) ON DELETE CASCADE,
    prior_value  FLOAT8 NOT NULL,
    new_value    FLOAT8 NOT NULL,
    -- Which outcome caused the movement, so a value in the history can always be
    -- traced back to the session whose ending produced it.
    outcome_id   UUID NOT NULL REFERENCES procedure_outcome (id) ON DELETE CASCADE,
    changed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Both endpoints of a movement lie in the closed unit interval, so a
    -- recorded movement can never describe a value the artifact could not hold.
    CONSTRAINT change_values_in_range CHECK (
        prior_value BETWEEN 0.0 AND 1.0 AND new_value BETWEEN 0.0 AND 1.0),
    -- A record claims a movement that occurred. An adjustment absorbed by
    -- clamping at a bound, and an abandoned outcome, both write nothing.
    CONSTRAINT change_actually_changed CHECK (prior_value != new_value),
    -- Ascending, because the history query reads a procedure's movements in the
    -- order they happened.
    INDEX change_by_procedure (procedure_id, changed_at ASC)
);

-- The closed unit interval, both ends admitted, and absence admitted too because
-- the kind equivalence below is what decides when absence is legal.
--
-- molt:own-transaction
ALTER TABLE derived_artifact DROP CONSTRAINT IF EXISTS derived_confidence_range;

-- molt:own-transaction
ALTER TABLE derived_artifact ADD CONSTRAINT derived_confidence_range CHECK (
    procedure_confidence IS NULL
    OR (procedure_confidence >= 0.0 AND procedure_confidence <= 1.0));

-- The equivalence: a confidence value is present for the learned-procedure kind
-- and for no other kind. Both directions are refused, which is what an
-- implication would have left open.
--
-- molt:own-transaction
ALTER TABLE derived_artifact DROP CONSTRAINT IF EXISTS derived_confidence_kind;

-- molt:own-transaction
ALTER TABLE derived_artifact ADD CONSTRAINT derived_confidence_kind CHECK (
    (kind = 'learned_procedure') = (procedure_confidence IS NOT NULL));

-- Serves both uses recall makes of the value: the descending order is the
-- tie-break between results at equal cosine distance, and the same ordering makes
-- the floor comparison a bounded range rather than a scan. Restricted to the
-- procedure kind, so it holds no summary and no baseline row.
--
-- molt:own-transaction
CREATE INDEX IF NOT EXISTS derived_procedure_confidence
    ON derived_artifact (procedure_confidence DESC)
    WHERE kind = 'learned_procedure';
