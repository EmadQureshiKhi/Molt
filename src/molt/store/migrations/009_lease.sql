-- Erasure ownership as a fenced lease, and the fencing generation carried on the
-- evidence a run writes.
--
-- The erasure guarantee is that exactly one owner acts for a tenant at a time and
-- that a superseded owner's write is refused by the database rather than merely
-- unlikely. Without a fence, a worker whose lease has lapsed can still append
-- dispositions and sign a certificate, and the certificate would then describe a
-- run that two workers jointly performed. The monotonic generation is the
-- mechanism: every guarded write states the generation it believes it holds, and
-- a statement naming anything other than the current generation persists nothing.
--
-- Five shapes here are load-bearing rather than incidental.
--
-- The generation is positive and only increases. It is assigned as the maximum
-- generation ever recorded for the tenant plus one, evaluated inside the granting
-- serialisable transaction, so it is monotonic across the whole history rather
-- than across current leases only. A zero or negative generation would leave the
-- ordering that fencing depends on undefined, so it is refused at write time.
--
-- The history index makes that maximum a single seek. The maximum is read on
-- every acquisition, so a scan over accumulated prior generations would put the
-- cost of granting a lease in proportion to how many leases the tenant has ever
-- held. Descending generation order within the tenant means the answer is the
-- first key the seek lands on.
--
-- The current-lease index is partial. Prior generations stay resident as history,
-- each closed and each naming the lease that replaced it, while the uniqueness
-- that matters applies only to the rows that are still current: at most one
-- unsuperseded lease exists per tenant, and the database is what enforces that.
-- This is the same shape the attribution history uses, for the same reason.
--
-- Closure is consistent or refused. A superseded lease carries both the instant
-- it was superseded and the lease that superseded it, and a current lease carries
-- neither. A row holding one without the other claims a supersession nobody can
-- follow, in the same spirit as the attribution total-closure rule.
--
-- The two idempotency indexes are what make finalisation idempotent. The lease
-- key is unique outright and the run key is unique among the rows that carry one,
-- so a repeated finalisation collides with the recorded attempt, returns the
-- result stored on the run row, and mutates nothing.
--
-- The expiry ordering check is the small remaining honesty condition: a lease
-- cannot expire before it was acquired, because an interval that runs backwards
-- describes no window in which the owner held anything.
--
-- Privileges for this table are not granted here. The grants for every table
-- added by this migration generation are carried together by a later migration,
-- so the privilege surface is readable in one place rather than assembled from
-- fragments spread across the generation.
--
-- The references below are created inline and therefore carry the platform's
-- generated constraint names. A later migration replaces each of them with a
-- restricting reference by that generated name, so naming them here would leave
-- that replacement adding a second reference beside the first.

CREATE TABLE IF NOT EXISTS erasure_lease (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id       UUID NOT NULL REFERENCES client (id),
    -- Who holds the lease: the worker identity a refusal names back to the
    -- operator, so a contended erasure reports whom to ask rather than only that
    -- it was refused.
    owner           STRING NOT NULL,
    -- The fence. Assigned as the per-tenant historical maximum plus one, and
    -- stated by every guarded write the holder issues.
    generation      INT8 NOT NULL,
    -- The key that makes a repeated finalisation a no-op rather than a second
    -- run.
    idempotency_key STRING NOT NULL,
    acquired_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    renewed_at      TIMESTAMPTZ NULL,
    -- The closure pair. Both null while the lease is current, both present once
    -- it has been replaced.
    superseded_at   TIMESTAMPTZ NULL,
    superseded_by   UUID NULL REFERENCES erasure_lease (id),
    CONSTRAINT lease_generation_positive CHECK (generation >= 1),
    CONSTRAINT lease_expiry_after_acquisition CHECK (expires_at > acquired_at),
    CONSTRAINT lease_closure_consistent CHECK (
        (superseded_at IS NULL AND superseded_by IS NULL)
        OR (superseded_at IS NOT NULL AND superseded_by IS NOT NULL)),
    -- The per-tenant generation maximum, read on every acquisition, is the first
    -- key of this index within the tenant rather than a scan over the history.
    INDEX lease_history_by_client (client_id, generation DESC)
);

-- At most one current lease per tenant. Partial, so the closed prior generations
-- accumulate beside the one row the constraint governs.
CREATE UNIQUE INDEX IF NOT EXISTS lease_current_unique
    ON erasure_lease (client_id) WHERE superseded_at IS NULL;

-- A granting attempt is identified once. A repeat of the same attempt collides
-- here instead of producing a second lease.
CREATE UNIQUE INDEX IF NOT EXISTS lease_idempotency_unique
    ON erasure_lease (idempotency_key);

-- The run states the generation it was performed under, and names the lease that
-- generation belongs to, so the ownership claim on a certificate is checkable
-- against the lease history rather than taken on trust.
ALTER TABLE erasure_run ADD COLUMN IF NOT EXISTS fencing_generation INT8 NULL;
ALTER TABLE erasure_run ADD COLUMN IF NOT EXISTS lease_id UUID NULL
    REFERENCES erasure_lease (id);
ALTER TABLE erasure_run ADD COLUMN IF NOT EXISTS idempotency_key STRING NULL;
-- The marker that makes a repeated finalisation a no-op, and the outcome such a
-- repeat returns unchanged.
ALTER TABLE erasure_run ADD COLUMN IF NOT EXISTS finalised_at TIMESTAMPTZ NULL;
ALTER TABLE erasure_run ADD COLUMN IF NOT EXISTS finalisation_result JSONB NULL;
-- Working-tier rows are erased for the tenant as one set-based statement and
-- accounted as one number. A disposition is evidence about content that mattered;
-- a working row is by construction content that did not, so it earns a count
-- rather than a record of its own.
ALTER TABLE erasure_run ADD COLUMN IF NOT EXISTS working_rows_deleted INT NOT NULL DEFAULT 0;

-- Unique among the runs that carry a key, so the rows written before this column
-- existed and the rows of runs that need no key are unaffected.
--
-- Applied in a transaction of its own, after the body of this migration has
-- committed. A partial index's predicate has to read a column the platform
-- already serves, and a column added earlier in the same transaction is not yet
-- visible in that sense, so the statement is refused there and accepted once the
-- column is established. Every statement in this file is guarded, so an
-- interrupted run re-applies the file whole without consequence.
-- molt:own-transaction
CREATE UNIQUE INDEX IF NOT EXISTS run_idempotency_unique
    ON erasure_run (idempotency_key) WHERE idempotency_key IS NOT NULL;

-- The generation on the evidence itself is what makes the fencing claim auditable
-- without consulting the process that ran: each disposition says which owner's
-- generation decided it, and the certificate says which generation finalised the
-- run.
ALTER TABLE disposition ADD COLUMN IF NOT EXISTS fencing_generation INT8 NULL;
ALTER TABLE erasure_certificate ADD COLUMN IF NOT EXISTS fencing_generation INT8 NULL;
