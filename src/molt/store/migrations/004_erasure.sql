-- Erasure evidence: the request, the run, the candidate sets, the per-artifact
-- dispositions, the backup record, the signed certificate, and the cluster audit
-- records covering the run window.
--
-- Every table here is write-once evidence about a governed mutation. The rows are
-- what a certificate is assembled from and what an independent verifier
-- re-derives, so the shapes below are chosen to make that derivation possible
-- from stored state alone rather than from the memory of the process that ran.
--
-- Four shapes are load-bearing rather than incidental.
--
-- The disposition's artifact identifier carries no foreign key. A disposition is
-- evidence about an artifact a hard delete removed, so it must outlive the row it
-- describes. A reference of any kind would either refuse the erasure it is
-- evidence of or vanish along with it, and both defeat the record.
--
-- The run's two thresholds are ordered by a check. Auto-inclusion admits a
-- candidate without asking the model; review sends it for adjudication. A run
-- whose auto-inclusion threshold sits above its review threshold describes no
-- coherent band, so it is refused at write time rather than discovered later.
--
-- The active-run index is partial. Every binding write asks whether an erasure is
-- in flight for a tenant, so that question has to be a single seek rather than a
-- scan over completed history, and the index admits only the in-flight rows.
--
-- The backup record holds the path value alongside separate taken and referenced
-- flags. The primary path issues a self-managed backup and records taken; the
-- fallback names the most recent managed backup and records referenced. A
-- certificate states which of the two happened and a verifier checks that claim,
-- so the two must be distinguishable from the row and can never both hold.
--
-- The references to the run are created inline and therefore carry the platform's
-- generated constraint names. A later migration replaces each of them with a
-- restricting reference by that generated name, so naming them here would leave
-- that replacement adding a second reference beside the first.

CREATE TABLE IF NOT EXISTS erasure_request (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id     UUID NOT NULL REFERENCES client (id),
    requester     STRING NOT NULL,
    justification STRING NOT NULL,
    submitted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status        STRING NOT NULL DEFAULT 'submitted',
    CONSTRAINT request_status_known CHECK (status IN (
        'submitted', 'running', 'completed', 'aborted'))
);

CREATE TABLE IF NOT EXISTS erasure_run (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id             UUID NOT NULL REFERENCES erasure_request (id),
    client_id              UUID NOT NULL REFERENCES client (id),
    requester              STRING NOT NULL,
    dry_run                BOOL NOT NULL DEFAULT false,
    status                 STRING NOT NULL DEFAULT 'running',
    phase                  STRING NOT NULL DEFAULT 'sweep',
    t_before               TIMESTAMPTZ NOT NULL,
    t_after                TIMESTAMPTZ NULL,
    auto_include_threshold FLOAT8 NOT NULL DEFAULT 0.20,
    review_threshold       FLOAT8 NOT NULL DEFAULT 0.45,
    backup_id              STRING NULL,
    backup_skipped         BOOL NOT NULL DEFAULT false,
    unembedded_count       INT NOT NULL DEFAULT 0,
    error_detail           STRING NULL,
    started_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at            TIMESTAMPTZ NULL,
    CONSTRAINT run_status_known CHECK (status IN ('running', 'completed', 'aborted')),
    CONSTRAINT run_phase_known CHECK (phase IN (
        'sweep', 'residue', 'disposition', 'certificate', 'done')),
    -- The band the residue phase adjudicates is the interval between the two
    -- thresholds, so the pair has to be ordered for the band to exist. The upper
    -- bound is the maximum cosine distance two vectors can stand apart.
    CONSTRAINT run_thresholds_ordered CHECK (
        auto_include_threshold >= 0.0
        AND review_threshold >= auto_include_threshold
        AND review_threshold <= 2.0),
    INDEX run_active_by_client (client_id) WHERE status = 'running'
);

CREATE TABLE IF NOT EXISTS erasure_candidate (
    run_id           UUID NOT NULL REFERENCES erasure_run (id) ON DELETE CASCADE,
    artifact_id      UUID NOT NULL,
    artifact_kind    STRING NOT NULL,
    content_digest   STRING NULL,
    selection_reason STRING NOT NULL,
    added_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT candidate_pk PRIMARY KEY (run_id, artifact_id),
    -- One value per way an artifact can enter the candidate set: the five
    -- statements of the explicit sweep, plus the semantic phase that extends it.
    CONSTRAINT candidate_reason_known CHECK (selection_reason IN (
        'session_scope', 'event_of_scoped_session', 'client_binding',
        'lineage_descendant', 'embedding_of_selected', 'semantic_residue'))
);

CREATE TABLE IF NOT EXISTS residue_candidate (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id            UUID NOT NULL REFERENCES erasure_run (id) ON DELETE CASCADE,
    artifact_id       UUID NOT NULL,
    artifact_kind     STRING NOT NULL,
    query_artifact_id UUID NOT NULL,
    cosine_distance   FLOAT8 NOT NULL,
    band              STRING NOT NULL,
    adjudicated       BOOL NOT NULL DEFAULT false,
    model_id          STRING NULL,
    prompt_digest     STRING NULL,
    classification    STRING NULL,
    reasoning         STRING NULL,
    included          BOOL NOT NULL,
    decision_reason   STRING NOT NULL,
    evaluated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT residue_band_known CHECK (band IN ('auto_include', 'review')),
    -- A candidate the auto-inclusion band admitted was never adjudicated, so it
    -- carries no classification at all rather than a defaulted one.
    CONSTRAINT residue_classification_known CHECK (
        classification IS NULL OR classification IN ('include', 'exclude')),
    CONSTRAINT residue_unique_per_run UNIQUE (run_id, artifact_id),
    INDEX residue_by_run (run_id, cosine_distance ASC)
);

CREATE TABLE IF NOT EXISTS disposition (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id           UUID NOT NULL REFERENCES erasure_run (id) ON DELETE CASCADE,
    -- Deliberately a plain column with no reference of any kind: a disposition
    -- describing a hard-deleted artifact has to survive that deletion, and a
    -- foreign key would make the evidence cascade away with the thing it is
    -- evidence of. Existence is not the claim this row makes; what happened is.
    artifact_id      UUID NOT NULL,
    artifact_kind    STRING NOT NULL,
    disposition      STRING NOT NULL,
    reason           STRING NOT NULL,
    selection_reason STRING NOT NULL,
    pre_digest       STRING NULL,
    post_digest      STRING NULL,
    -- The tenant slugs bound before and after the decision. A surgical
    -- redaction's whole claim is that the erased tenant's binding is gone and
    -- every other tenant's binding survived, and these two arrays are what let a
    -- verifier check that claim without reconstructing bindings the run deleted.
    bindings_before  STRING[] NOT NULL DEFAULT ARRAY[]::STRING[],
    bindings_after   STRING[] NOT NULL DEFAULT ARRAY[]::STRING[],
    decided_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT disposition_known CHECK (disposition IN (
        'hard_delete', 'surgical_redaction', 'retained')),
    CONSTRAINT disposition_unique_per_run UNIQUE (run_id, artifact_id),
    INDEX disposition_by_run (run_id, disposition)
);

CREATE TABLE IF NOT EXISTS run_session (
    run_id                UUID NOT NULL REFERENCES erasure_run (id) ON DELETE CASCADE,
    session_id            UUID NOT NULL,
    terminal_chain_digest STRING NULL,
    terminal_seq          INT NULL,
    row_count             INT NULL,
    CONSTRAINT run_session_pk PRIMARY KEY (run_id, session_id)
);

CREATE TABLE IF NOT EXISTS backup_record (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id      UUID NOT NULL REFERENCES erasure_run (id) ON DELETE CASCADE,
    backup_id   STRING NULL,
    -- Which of the two paths produced this row. Null where no path ran, which is
    -- the skipped case and the case where the run aborted before either.
    backup_path STRING NULL,
    target_uri  STRING NULL,
    taken_at    TIMESTAMPTZ NULL,
    -- The statement issued on the primary path, or the argument vector invoked on
    -- the fallback path, recorded verbatim so the claim is reproducible.
    command     STRING NOT NULL,
    -- A backup this run created.
    taken       BOOL NOT NULL DEFAULT false,
    -- A backup that already existed and this run named. Evidence that a backup
    -- exists is not evidence that Molt made one, and a certificate says which.
    referenced  BOOL NOT NULL DEFAULT false,
    status      STRING NOT NULL,
    detail      STRING NULL,
    CONSTRAINT backup_status_known CHECK (status IN ('succeeded', 'failed', 'skipped')),
    CONSTRAINT backup_path_known CHECK (
        backup_path IS NULL OR backup_path IN ('self_managed', 'managed_referenced')),
    -- The two flags are alternatives, never both: one says this run issued the
    -- backup, the other says it pointed at somebody else's.
    CONSTRAINT backup_flags_exclusive CHECK (NOT (taken AND referenced)),
    -- And each flag agrees with the path it belongs to, so the recorded path and
    -- the recorded flags cannot tell two different stories. A row where neither
    -- flag holds is free to name the path that was attempted and failed.
    CONSTRAINT backup_flag_matches_path CHECK (
        (NOT taken OR backup_path = 'self_managed')
        AND (NOT referenced OR backup_path = 'managed_referenced'))
);

CREATE TABLE IF NOT EXISTS erasure_certificate (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id            UUID NOT NULL REFERENCES erasure_run (id) ON DELETE CASCADE,
    payload           JSONB NOT NULL,
    canonical_digest  STRING NOT NULL,
    signature         BYTES NULL,
    kms_key_id        STRING NULL,
    signing_algorithm STRING NULL,
    s3_bucket         STRING NULL,
    s3_key            STRING NULL,
    s3_version_id     STRING NULL,
    -- A signed payload that no object store accepted is still evidence, so the
    -- storage outcome is a state on the row rather than a reason to discard it.
    storage_status    STRING NOT NULL DEFAULT 'pending',
    storage_detail    STRING NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT certificate_storage_status_known CHECK (storage_status IN (
        'pending', 'stored', 'failed')),
    CONSTRAINT certificate_unique_per_run UNIQUE (run_id)
);

CREATE TABLE IF NOT EXISTS audit_log_snapshot (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id       UUID NOT NULL REFERENCES erasure_run (id) ON DELETE CASCADE,
    window_start TIMESTAMPTZ NOT NULL,
    window_end   TIMESTAMPTZ NOT NULL,
    records      JSONB NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
