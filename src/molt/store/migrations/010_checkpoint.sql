-- Signed ledger checkpoints: the window, the root digest, the signature that was
-- produced outside the cluster, and the per-Session terminal digests the window
-- committed to.
--
-- Why these two tables exist at all is worth stating, because the per-Session
-- hash chain already detects tampering. It detects the tampering an editor of one
-- row leaves behind. It does not detect a consistent rewrite: a principal holding
-- administrator privilege on the cluster can recompute a whole Session's chain
-- after changing its content, and the recomputed chain verifies, because a chain
-- is self-consistent by construction. A checkpoint closes that gap by committing
-- to the terminal digest of every Session in a window and having that commitment
-- signed by a key the cluster holds no access to. A rewrite consequently stays
-- detectable by a party that does not trust the cluster's administrator, which is
-- coverage no in-cluster mechanism can give itself.
--
-- Four shapes here are load-bearing rather than incidental.
--
-- Neither table carries Row-Level TTL, and that absence is the point rather than
-- an omission. The content tables expire their rows because retention obliges it;
-- a checkpoint's entire value is that it stays checkable after the rows it commits
-- to have gone, so an expiring checkpoint would be evidence that disappears
-- exactly when it is wanted. A table carries no TTL unless a storage parameter
-- configures one, so the correct statement here is no statement — stated in words
-- so that a later reader adds none.
--
-- The per-Session digests are recorded as they stood at checkpoint time. Without
-- them a root-digest mismatch would say only that something inside the window
-- moved. With them the verifier names every Session whose terminal digest differs
-- from the recorded one, and for each of those looks up the dispositions that
-- account for the difference, so a governed erasure is explained rather than
-- reported as tampering.
--
-- A covered Session's identifier carries no foreign key, deliberately, for the
-- same reason a disposition's artifact identifier carries none: the checkpoint
-- has to outlive the Session it covered. A reference would either refuse the
-- authorised erasure of that Session or vanish along with it, and either outcome
-- destroys the record whose whole purpose is to remain checkable afterwards.
--
-- The index over descending window ends serves one query that a certificate
-- cannot be assembled without: the most recent checkpoint whose window ends
-- before the instant the run began reading. That is a leading-column range scan
-- in the index's own order, so it stays a bounded seek as checkpoint history
-- accumulates rather than a scan that lengthens with it.
--
-- The reference to the checkpoint is created inline and therefore carries the
-- platform's generated constraint name. A later migration replaces it by that
-- generated name, so naming it here would leave that replacement adding a second
-- reference beside the first. Privileges are granted by a later migration too,
-- which is also where `UPDATE` and `DELETE` on both tables are revoked from every
-- role, because a checkpoint that can be rewritten commits to nothing.

CREATE TABLE IF NOT EXISTS ledger_checkpoint (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    window_start          TIMESTAMPTZ NOT NULL,
    window_end            TIMESTAMPTZ NOT NULL,
    covered_session_count INT NOT NULL,
    root_digest           STRING NOT NULL,
    signature             BYTES NOT NULL,
    -- Which key signed, and under which algorithm. A verifier retrieves that
    -- key's public half and checks the signature itself rather than asking the
    -- key service to check it, so verification survives the loss of permission to
    -- call that service, and both values are what make the check reproducible.
    kms_key_id            STRING NOT NULL,
    signing_algorithm     STRING NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- A window whose end precedes its start covers no interval, so it names no
    -- set of Sessions and its root digest commits to nothing.
    CONSTRAINT checkpoint_window_ordered CHECK (window_end > window_start),
    -- The same width the ledger's own digests are held at: a hexadecimal SHA-256
    -- digest is sixty-four characters, and a value of any other length is not one.
    CONSTRAINT checkpoint_digest_hex CHECK (length(root_digest) = 64),
    -- A window holding no Session is legitimate and counts zero; a negative count
    -- describes nothing.
    CONSTRAINT checkpoint_count_non_negative CHECK (covered_session_count >= 0),
    -- Serves the certificate's lookup of the most recent checkpoint whose window
    -- ended before the run's reading instant, in the index's own order.
    INDEX checkpoint_by_window_end (window_end DESC)
);

CREATE TABLE IF NOT EXISTS checkpoint_session (
    checkpoint_id         UUID NOT NULL REFERENCES ledger_checkpoint (id) ON DELETE RESTRICT,
    -- Deliberately a plain column with no reference of any kind. The checkpoint
    -- must remain checkable after an authorised erasure has removed the Session
    -- this row names, and a foreign key would make this evidence either refuse
    -- that erasure or disappear with it.
    session_id            UUID NOT NULL,
    -- The Session's terminal chain digest and terminal sequence as they stood
    -- when the checkpoint was taken. A later verification compares the live rows
    -- against these two values, so they are the recorded past rather than a
    -- pointer at a present that may since have moved.
    terminal_chain_digest STRING NOT NULL,
    terminal_seq          INT NOT NULL,
    CONSTRAINT checkpoint_session_pk PRIMARY KEY (checkpoint_id, session_id)
);
