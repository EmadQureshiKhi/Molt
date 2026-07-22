-- The working tier: short-lived agent scratch state, and the only tier in this
-- schema whose rows nothing is permitted to depend on.
--
-- Every other tier exists because something has to remain readable later. This
-- one exists because something has to be forgettable. An agent holds a plan under
-- revision, a partially assembled answer, a cursor into a file it is walking; all
-- of that is state the agent needs while it works and nobody needs afterwards.
-- Keeping it in the durable tiers would make each of those a governed artifact
-- with a lineage, an attribution, and an erasure obligation, which is a large
-- amount of ceremony for a value whose whole nature is to be superseded within
-- the hour.
--
-- Five shapes here are load-bearing.
--
-- The key is the Session identifier and the scratch key together, so a write is
-- an upsert on the primary key and a read is a single point lookup. That is what
-- makes revision cheap: a plan rewritten forty times leaves one row, not forty.
-- A tier that accumulated versions would be a history, and a history is exactly
-- what this tier must not become, because a history is something a later reader
-- can depend on.
--
-- The expiry is a column with a default of the configured interval, and the
-- interval is 3600 seconds. Holding it as a column rather than as a fixed
-- interval after insertion keeps the shape identical to the content tables, so a
-- writer that wants a shorter-lived scratch value sets the column and needs no
-- schema change. The default is what makes the ordinary case require nothing of
-- the writer at all.
--
-- The job cron is hourly, and this is the one difference from the content tables
-- that matters most. Those tables are swept daily, which is a comfortable
-- tolerance for a record an auditor reads weeks later. Applying that same daily
-- schedule here would leave a row whose stated lifetime is one hour resident for
-- up to a day after it expired, and the tier's disposability would then be a
-- claim in a document rather than a property of the cluster. An hourly sweep
-- against an hourly interval is what makes expiry observable: a row past its
-- expiry leaves within the same order of magnitude as its own lifetime, and it
-- leaves because the cluster removes it, with nothing scheduled outside the
-- cluster involved. Three TTL postures now exist in this schema for three
-- different reasons -- daily on the content tables, none at all on the checkpoint
-- tables, hourly here -- and this is deliberately the disposable one.
--
-- The delete batch size matches the content tables. The sweep competes with
-- foreground traffic for the same ranges, and a frequent job is a stronger reason
-- to keep each bite small rather than a weaker one.
--
-- The `artifact_ref` view is extended by nothing here, and that omission is the
-- enforcement rather than an oversight. That view spans exactly three kinds --
-- an event, a Session, and a derived artifact -- and a statement inserting a
-- lineage edge proves its parent exists by joining it. A kind the view does not
-- return therefore cannot be named as a lineage parent, cannot be swept into an
-- erasure candidate set that is built from the same artifact kinds, and cannot
-- become the subject of an attribution binding. Nothing in the working tier is
-- reachable from the provenance, action, or attribution tiers, and the reason is
-- structural: the join that would have to succeed returns no row. A later reader
-- adding this table to that view would silently convert a disposable scratch row
-- into a governed artifact, so the correct statement here is no statement --
-- stated in words so that none is added.
--
-- Two notes on shape. The Session reference is declared inline and so carries the
-- platform's generated constraint name; a later migration replaces it by that
-- generated name, so naming it here would leave that replacement adding a second
-- reference beside the first.
--
-- And the expiry configuration is marked as needing a transaction of its own,
-- which was established by probing rather than by assumption, and the probe result
-- is worth recording because it is nastier than the refusals earlier migrations
-- ran into. Configuring row-level expiry on a table that came into being earlier
-- in the same transaction is not refused. It reports success, the transaction
-- commits, and the storage parameters are simply absent from the committed
-- descriptor afterwards. A reader who checked only for an error would conclude the
-- tier expires its rows while the cluster in fact sweeps nothing, which is the one
-- failure this tier cannot tolerate quietly. Applied after the body has committed,
-- the configuration lands and reads back. The runner writes this file's history
-- row only once the marked statement has succeeded, so an interrupted application
-- is re-applied whole rather than leaving a table without its expiry.
--
-- Privileges are granted by a later migration, which is where every table this
-- generation adds is granted at once.

CREATE TABLE IF NOT EXISTS working_memory (
    -- The owning Session, cascading on deletion. Scratch state is recomputable
    -- by construction, so a removed Session takes its scratch with it rather
    -- than refusing to go; every tier holding evidence takes the opposite
    -- posture, and the difference is the whole point of the tier.
    session_id  UUID NOT NULL REFERENCES session (id) ON DELETE CASCADE,
    -- The name the agent gave this piece of scratch. Opaque here: the tier
    -- imposes no vocabulary, because a tier that constrained the keys would be
    -- making a durable statement about them.
    scratch_key STRING NOT NULL,
    -- The owning tenant, carried on the row rather than reached through the
    -- Session, so the purge erasure issues is a single-column predicate served
    -- by one index instead of a join.
    client_id   UUID NOT NULL REFERENCES client (id),
    value       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- The instant after which this row is garbage. The default is the configured
    -- working-tier interval of 3600 seconds; a writer needing a shorter life sets
    -- the column and needs no schema change to do it.
    expires_at  TIMESTAMPTZ NOT NULL DEFAULT (now() + INTERVAL '3600 seconds'),
    -- Session and scratch key together, so a repeated write overwrites in place
    -- and the tier accumulates nothing.
    CONSTRAINT working_memory_pk PRIMARY KEY (session_id, scratch_key),
    -- Serves the single set-based statement erasure issues against this table at
    -- run start: delete every row of one tenant, returning one aggregate count
    -- that is recorded on the run row. One statement, one number, no per-row
    -- dispositions -- a disposition is evidence about content that mattered, and
    -- a working row is by construction content that did not.
    INDEX working_by_client (client_id)
);

-- Row-level expiry, hourly against an hourly interval, in a small batch.
-- Re-declaring the same storage parameters on a table that already carries them
-- leaves the configuration exactly as it was, so this is re-runnable.
--
-- molt:own-transaction
ALTER TABLE working_memory SET (
    ttl_expiration_expression = 'expires_at',
    ttl_job_cron = '@hourly',
    ttl_delete_batch_size = 500
);
