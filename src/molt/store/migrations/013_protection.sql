-- Structural protection of the audit record: evidence refuses to be cascaded
-- away, recomputable rows cascade freely, and the two self-references are
-- removed because the transaction rather than the constraint is what makes them
-- honest.
--
-- Deleting a row of memory content is a governed operation with a request, a
-- lease, a candidate set, a disposition per artifact, and a signed certificate
-- behind it. Deleting a row of evidence *about* that operation is not something
-- any principal should be able to do by accident, and cascading deletes were
-- exactly how it could have happened: the references the earlier migrations
-- created inline carry `ON DELETE CASCADE` or the platform's default, so
-- removing one run row would silently remove the whole record of what that run
-- touched. Every reference to an erasure request, an erasure run, an erasure
-- lease, or a ledger checkpoint is re-created below with `ON DELETE RESTRICT`,
-- which turns that accident into a refusal naming the referencing table and the
-- referencing row count (Requirements 46.1, 46.2).
--
-- The division is between evidence and recomputable derivation, and it is drawn
-- per table rather than uniformly.
--
-- Restricted, because the row is the only record of something that happened: a
-- run against its request and its lease, the candidate set and the residue
-- candidates a sweep selected, the dispositions a certificate's completeness
-- claim rests on, the terminal chain digest per touched session, the backup
-- evidence answering whether the erasure was reversible when it ran, the signed
-- certificate itself, the cluster audit records covering the run window, and the
-- per-session digests that localise a checkpoint disagreement.
--
-- Cascading, because the row is a function of something else that survives: an
-- embedding is a recomputable function of an artifact's text and the configured
-- model, a lineage edge whose child is gone describes nothing and the derivation
-- it recorded is carried on the disposition and in the certificate's lineage
-- subgraph, and a working-tier scratch row for a session that no longer exists
-- is disposable by construction (Requirement 46.3).
--
-- Three identifiers deliberately carry no reference at all, and this file adds
-- none. A disposition's artifact identifier is the first, and it is the shape the
-- other two are argued from: a disposition is evidence about an artifact a hard
-- delete removed, so it must outlive that artifact, and a reference of any kind
-- would either refuse the erasure the disposition is evidence of or vanish along
-- with it (Requirement 46.4). A checkpoint's covered session identifier is the
-- second, for the same reason. The two supersession identifiers below are the
-- third and fourth, for a reason of ordering rather than of survival.
--
-- The two self-references, and why they are dropped here.
--
-- Closing a version and inserting its successor is two mutations of one table.
-- This cluster refuses a single statement that performs two mutations of one
-- table through a common table expression, so the pair cannot be one statement:
-- a supersession is two ordered statements inside one transaction at the
-- SERIALIZABLE isolation level, the close first and the insert second. That
-- shape briefly requires the closed row to name a successor that the next
-- statement has not yet inserted, and this cluster checks each foreign key per
-- statement with no deferred checking available anywhere, so there is no
-- arrangement of the two statements in which a self-referencing constraint
-- survives. Reversing the order does not help: inserting the successor first
-- leaves two rows current for one pair, which the partial unique index that
-- makes the history a history refuses. The integrity of each superseding
-- identifier therefore comes from the transaction, which commits both statements
-- or neither, rather than from the constraint -- the reasoning already applied to
-- the disposition's artifact identifier and to the checkpoint's covered session
-- identifier (Requirements 43.12, 43.13, 44.17, 44.18, 46.9).
--
-- Landing both removals in this file, rather than reworking the two migrations
-- that added them, is what keeps the runner able to run. An applied migration is
-- never edited: the runner records the digest of each file it applies and refuses
-- the next run when a recorded digest no longer matches its file. Editing either
-- earlier file would break its recorded digest and stop every run rather than
-- report a clean no-op, so the correction arrives as a new numbered file and no
-- already-applied file moves.
--
-- Two notes on shape, both forced by the platform.
--
-- Every statement below is marked as needing a transaction of its own. A
-- constraint cannot be removed and re-added under the same name inside one
-- transaction, because the removal is not yet visible when the addition is
-- checked, and the second application of this file does exactly that: the
-- guarded drop names the constraint this file's own previous application added.
-- Marking the statements is what makes the file re-runnable rather than
-- re-runnable only once.
--
-- Each reference is consequently removed by two names and added by one. The
-- first name is the one the platform generated for the inline declaration in the
-- earlier migration; the second is the name this file gives the replacement, so
-- a re-application removes its own previous work before repeating it and the
-- committed state is the same either way. The runner writes this file's history
-- row only once every marked statement has succeeded, so an interrupted
-- application is re-applied whole.

-- A run is the execution of a request, and a request whose runs still exist is
-- history rather than a draft.
--
-- molt:own-transaction
ALTER TABLE erasure_run
    DROP CONSTRAINT IF EXISTS erasure_run_request_id_fkey,
    DROP CONSTRAINT IF EXISTS erasure_run_request_fk;

-- molt:own-transaction
ALTER TABLE erasure_run ADD CONSTRAINT erasure_run_request_fk
    FOREIGN KEY (request_id) REFERENCES erasure_request (id) ON DELETE RESTRICT;

-- The lease record is the proof that the finalising worker held ownership at the
-- generation the certificate states, so it outlives nothing quietly.
--
-- molt:own-transaction
ALTER TABLE erasure_run
    DROP CONSTRAINT IF EXISTS erasure_run_lease_id_fkey,
    DROP CONSTRAINT IF EXISTS erasure_run_lease_fk;

-- molt:own-transaction
ALTER TABLE erasure_run ADD CONSTRAINT erasure_run_lease_fk
    FOREIGN KEY (lease_id) REFERENCES erasure_lease (id) ON DELETE RESTRICT;

-- The candidate set is the record of what the sweep selected, and a certificate's
-- completeness claim rests on it.
--
-- molt:own-transaction
ALTER TABLE erasure_candidate
    DROP CONSTRAINT IF EXISTS erasure_candidate_run_id_fkey,
    DROP CONSTRAINT IF EXISTS erasure_candidate_run_fk;

-- molt:own-transaction
ALTER TABLE erasure_candidate ADD CONSTRAINT erasure_candidate_run_fk
    FOREIGN KEY (run_id) REFERENCES erasure_run (id) ON DELETE RESTRICT;

-- Distances, bands, and adjudication reasoning are the only record of why a
-- borderline artifact was included or left alone.
--
-- molt:own-transaction
ALTER TABLE residue_candidate
    DROP CONSTRAINT IF EXISTS residue_candidate_run_id_fkey,
    DROP CONSTRAINT IF EXISTS residue_candidate_run_fk;

-- molt:own-transaction
ALTER TABLE residue_candidate ADD CONSTRAINT residue_candidate_run_fk
    FOREIGN KEY (run_id) REFERENCES erasure_run (id) ON DELETE RESTRICT;

-- The dispositions are the certificate's substance; losing them makes the
-- certificate unverifiable.
--
-- No reference is added to this table's artifact identifier, here or anywhere. A
-- disposition must outlive the artifact it describes, so that column stays a
-- plain value (Requirement 46.4).
--
-- molt:own-transaction
ALTER TABLE disposition
    DROP CONSTRAINT IF EXISTS disposition_run_id_fkey,
    DROP CONSTRAINT IF EXISTS disposition_run_fk;

-- molt:own-transaction
ALTER TABLE disposition ADD CONSTRAINT disposition_run_fk
    FOREIGN KEY (run_id) REFERENCES erasure_run (id) ON DELETE RESTRICT;

-- Terminal chain digests per touched session are what a verifier re-derives.
--
-- molt:own-transaction
ALTER TABLE run_session
    DROP CONSTRAINT IF EXISTS run_session_run_id_fkey,
    DROP CONSTRAINT IF EXISTS run_session_run_fk;

-- molt:own-transaction
ALTER TABLE run_session ADD CONSTRAINT run_session_run_fk
    FOREIGN KEY (run_id) REFERENCES erasure_run (id) ON DELETE RESTRICT;

-- The backup evidence answers whether an erasure was reversible at the moment it
-- ran, which is a question no later reconstruction can answer.
--
-- molt:own-transaction
ALTER TABLE backup_record
    DROP CONSTRAINT IF EXISTS backup_record_run_id_fkey,
    DROP CONSTRAINT IF EXISTS backup_record_run_fk;

-- molt:own-transaction
ALTER TABLE backup_record ADD CONSTRAINT backup_record_run_fk
    FOREIGN KEY (run_id) REFERENCES erasure_run (id) ON DELETE RESTRICT;

-- The signed document must not be orphaned, and must not be removable by
-- removing the run it describes.
--
-- molt:own-transaction
ALTER TABLE erasure_certificate
    DROP CONSTRAINT IF EXISTS erasure_certificate_run_id_fkey,
    DROP CONSTRAINT IF EXISTS erasure_certificate_run_fk;

-- molt:own-transaction
ALTER TABLE erasure_certificate ADD CONSTRAINT erasure_certificate_run_fk
    FOREIGN KEY (run_id) REFERENCES erasure_run (id) ON DELETE RESTRICT;

-- Cluster audit records covering the run window are third-party corroboration of
-- what the run did, so they are protected like the run's own evidence.
--
-- molt:own-transaction
ALTER TABLE audit_log_snapshot
    DROP CONSTRAINT IF EXISTS audit_log_snapshot_run_id_fkey,
    DROP CONSTRAINT IF EXISTS audit_log_snapshot_run_fk;

-- molt:own-transaction
ALTER TABLE audit_log_snapshot ADD CONSTRAINT audit_log_snapshot_run_fk
    FOREIGN KEY (run_id) REFERENCES erasure_run (id) ON DELETE RESTRICT;

-- Per-session digests are what localise a checkpoint disagreement, so a
-- checkpoint cannot be reduced to its root digest by a delete. The inline
-- declaration already restricted; it is re-created under a name of this file's
-- own so that every protected reference is named by one convention and readable
-- as one set.
--
-- molt:own-transaction
ALTER TABLE checkpoint_session
    DROP CONSTRAINT IF EXISTS checkpoint_session_checkpoint_id_fkey,
    DROP CONSTRAINT IF EXISTS checkpoint_session_checkpoint_fk;

-- molt:own-transaction
ALTER TABLE checkpoint_session ADD CONSTRAINT checkpoint_session_checkpoint_fk
    FOREIGN KEY (checkpoint_id) REFERENCES ledger_checkpoint (id) ON DELETE RESTRICT;

-- The superseding attribution version reference, added by the attribution
-- migration and removed here for the ordering reason argued above: the closing
-- statement names a successor the following statement inserts, and the
-- transaction is what makes that reference real.
--
-- molt:own-transaction
ALTER TABLE client_binding DROP CONSTRAINT IF EXISTS binding_superseded_by_fkey;

-- The superseding lease reference, added by the lease migration and removed for
-- the same reason. What keeps the ordering honest here is `lease_current_unique`:
-- inserting the successor before closing the incumbent would leave two leases
-- unsuperseded for one tenant, which that partial unique index refuses.
--
-- molt:own-transaction
ALTER TABLE erasure_lease DROP CONSTRAINT IF EXISTS erasure_lease_superseded_by_fkey;

-- An embedding is a recomputable function of an artifact's text and the
-- configured model. Rebuilding one costs one provider call; keeping a stale one
-- costs correctness, so the tenant's removal takes its vectors with it.
--
-- molt:own-transaction
ALTER TABLE embedding
    DROP CONSTRAINT IF EXISTS embedding_client_id_fkey,
    DROP CONSTRAINT IF EXISTS embedding_client_fk;

-- molt:own-transaction
ALTER TABLE embedding ADD CONSTRAINT embedding_client_fk
    FOREIGN KEY (client_id) REFERENCES client (id) ON DELETE CASCADE;

-- An edge whose child is gone describes nothing. The derivation it recorded is
-- carried on the disposition and in the certificate's lineage subgraph, so the
-- evidence survives the edge.
--
-- molt:own-transaction
ALTER TABLE lineage_edge
    DROP CONSTRAINT IF EXISTS lineage_edge_child_id_fkey,
    DROP CONSTRAINT IF EXISTS lineage_edge_child_fk;

-- molt:own-transaction
ALTER TABLE lineage_edge ADD CONSTRAINT lineage_edge_child_fk
    FOREIGN KEY (child_id) REFERENCES derived_artifact (id) ON DELETE CASCADE;

-- Scratch state for a session that no longer exists is disposable by definition,
-- and the working tier's whole purpose is that nothing depends on it surviving.
--
-- molt:own-transaction
ALTER TABLE working_memory
    DROP CONSTRAINT IF EXISTS working_memory_session_id_fkey,
    DROP CONSTRAINT IF EXISTS working_memory_session_fk;

-- molt:own-transaction
ALTER TABLE working_memory ADD CONSTRAINT working_memory_session_fk
    FOREIGN KEY (session_id) REFERENCES session (id) ON DELETE CASCADE;
