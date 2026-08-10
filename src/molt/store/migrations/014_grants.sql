-- Privileges for every table the second migration generation adds, and the three
-- writable-column guards those privileges depend on.
--
-- This file closes the second generation the way the roles migration closed the
-- first. The reason the grants live here rather than beside the tables they
-- govern is the runner's own rule: an applied migration is never edited. Folding
-- these statements back into the roles migration would change that file's bytes,
-- the digest recorded for it would no longer match, and the runner would refuse
-- the next run outright rather than report a clean no-op. Collecting the
-- generation's privileges in one later file is what keeps the privilege surface
-- readable in one place while leaving every applied file untouched.
--
-- Two of the statements below are obligations rather than conveniences, and both
-- are worth naming because a reader skimming a wall of grants would not see them.
--
-- The first is the writer's read of the erasure lease. The roles migration could
-- not grant it: the lease table does not exist until a later migration in this
-- generation, and that file grants only on the tables the first generation
-- creates. The writer performs the erasure guard read against the lease table on
-- every attribution write, so without this grant the ordinary write path fails on
-- a missing privilege rather than on anything to do with erasure. The grant is
-- required, not tidy.
--
-- The second is the shape of every column confinement here. GRANT on this cluster
-- admits a table and a privilege and nothing finer -- there is no column list to
-- grant, and a view narrowed to the writable columns is not updatable either --
-- so each confinement is expressed as a guard that runs before every update and
-- refuses a statement changing a column the acting role may not change, with the
-- table-level GRANT UPDATE alongside it. The enforcement point is still the
-- database rather than the writing statement, which is what the requirements ask
-- for; only its shape differs from a column list. Three confinements arrive with
-- this generation and so three guards are attached below.
--
-- Each guard follows the shape the roles migration established, for the reasons
-- it gives. It names the columns that are immutable rather than the columns that
-- are writable, so a column a later migration adds is writable unless a later
-- guard protects it. It exempts the administrative path, because a database
-- administrator can already drop the table and a guard pretending otherwise would
-- be theatre; the answer to a hostile administrator is the externally signed
-- checkpoint, not a trigger. And it is three statements -- a removal, a
-- definition, and an attachment, in that order -- because this database refuses
-- to replace a definition a live trigger points at, so the attachment comes off
-- first and the trio is re-runnable. Every one of those statements carries the
-- marker asking the runner for a transaction of its own: attaching and removing a
-- trigger are served only by the newer schema changer, and that changer is
-- unreachable from a multi-statement transaction. The marker sits immediately
-- above the statement it applies to and nowhere else in this file.
--
-- Every statement here is re-runnable, which matters because a file holding
-- marked statements has its history row written only after those statements
-- succeed, so an interrupted run re-applies the whole file. A grant and a
-- revocation are both re-issuable with no effect the second time, a guard
-- function is replaced rather than added, and a guard trigger is removed by name
-- before it is attached.

-- The capture and ingest path. It writes scratch state, records that a procedure
-- was retrieved and how the session that used it ended, and moves a procedure's
-- standing. Everything else it does to these tables is a read.
GRANT SELECT, INSERT ON TABLE working_memory TO molt_writer;
-- Confined by the working-tier guard below to the value, its update instant, and
-- its expiry. A write to this tier is an upsert on the primary key, so the
-- confinement is what the upsert's update half runs into.
GRANT UPDATE ON TABLE working_memory TO molt_writer;
GRANT SELECT, INSERT ON TABLE procedure_retrieval, procedure_outcome,
    procedure_confidence_change TO molt_writer;

-- The read the roles migration could not grant, because the lease table arrives
-- later in this generation. The writer performs the erasure guard read against
-- this table on every attribution write.
GRANT SELECT ON TABLE erasure_lease TO molt_writer;

-- Standing may move; bodies, digests, revisions, and tenancy may not. Confined by
-- the artifact guard below, which is what makes the confinement a database fact
-- rather than a convention of the writing statement.
GRANT UPDATE ON TABLE derived_artifact TO molt_writer;

-- The erasure path. It owns the lease, purges the working tier for a tenant as
-- one set-based statement, and writes checkpoints.
GRANT SELECT, INSERT, UPDATE ON TABLE erasure_lease TO molt_eraser;
-- A working row is content that by construction did not matter, so erasure
-- removes it outright and accounts for it as one aggregate count. No UPDATE here:
-- the erasure path revises no scratch value, it only deletes.
GRANT SELECT, DELETE ON TABLE working_memory TO molt_eraser;
GRANT INSERT ON TABLE ledger_checkpoint, checkpoint_session TO molt_eraser;

-- A checkpoint is evidence every role may check and no role may write over. The
-- read is granted to all four roles: the certificate builder names the most
-- recent checkpoint preceding a run, the independent verifier recomputes its root
-- digest, and both do so with the privileges they already hold.
GRANT SELECT ON TABLE ledger_checkpoint, checkpoint_session
    TO molt_writer, molt_eraser, molt_reader, molt_watcher;

-- The read-only path. SELECT and nothing else, on the lease history, the
-- procedural usage history, and the checkpoints granted above. This is what makes
-- the no-mutation guarantee of the independent verifier, the sensitivity
-- analyser, the memory-protocol server, and the auditor views a privilege fact.
GRANT SELECT ON TABLE erasure_lease, procedure_retrieval, procedure_outcome,
    procedure_confidence_change TO molt_reader;

-- Audit evidence is not deletable by the role that performs erasures. Erasure
-- removes memory content, never the record of having removed it. No grant in this
-- generation or the last conferred DELETE on any of these tables, so this removes
-- a privilege that was never held -- stated all the same, because an absence
-- nobody wrote down is an absence a later grant can undo without anyone noticing.
--
-- This is the privilege half of the referential protection the previous migration
-- put in place, and neither half is sufficient alone: a restricting reference says
-- nothing about deleting a row nothing references, and a revoked privilege says
-- nothing about a cascade the database performs on the role's behalf.
REVOKE DELETE ON TABLE erasure_request, erasure_run, erasure_candidate,
    residue_candidate, disposition, run_session, backup_record, erasure_certificate,
    audit_log_snapshot FROM molt_eraser;

-- No role may edit or remove a checkpoint. A checkpoint's whole value is that it
-- commits to the state of every session in a window and stays checkable
-- afterwards; one that any Molt principal could rewrite or drop would commit to
-- nothing, and the coverage it extends beyond a cluster administrator would
-- collapse to the coverage the hash chain already gives itself.
REVOKE UPDATE, DELETE ON TABLE ledger_checkpoint, checkpoint_session
    FROM molt_writer, molt_eraser, molt_reader, molt_watcher;

-- The working-tier guard. The value, the instant it was last written, and the
-- instant it expires are writable; the owning session, the scratch key, and the
-- owning tenant are not. The first two are the primary key and moving them would
-- turn an upsert into a silent relocation of one agent's scratch onto another
-- key; the third is tenancy, and tenancy is unwritable in every tier of this
-- schema.
--
-- molt:own-transaction
DROP TRIGGER IF EXISTS molt_working_memory_update_scope_guard ON working_memory;

-- The body is a quoted literal rather than dollar-quoted, because the runner
-- splits a migration into statements on semicolons outside quoted text and a
-- dollar-quoted body would be split at every statement it holds. A single quote
-- inside the body is therefore doubled.
--
-- molt:own-transaction
CREATE OR REPLACE FUNCTION molt_working_memory_update_scope() RETURNS TRIGGER
    LANGUAGE PLpgSQL AS '
BEGIN
    IF pg_has_role(current_user, ''admin'', ''MEMBER'') THEN
        RETURN NEW;
    END IF;
    IF (NEW).session_id IS DISTINCT FROM (OLD).session_id
        OR (NEW).scratch_key IS DISTINCT FROM (OLD).scratch_key
        OR (NEW).client_id IS DISTINCT FROM (OLD).client_id THEN
        RAISE EXCEPTION ''the key and tenancy columns of a working row are not writable'';
    END IF;
    RETURN NEW;
END
';

-- molt:own-transaction
CREATE TRIGGER molt_working_memory_update_scope_guard
    BEFORE UPDATE ON working_memory
    FOR EACH ROW EXECUTE FUNCTION molt_working_memory_update_scope();

-- The artifact guard. Two confinements in one, because two roles hold UPDATE on
-- this table for two unrelated reasons and a guard that treated them alike would
-- have to permit the looser of the two to both.
--
-- Identity and tenancy are unwritable by every role: the identifier, the kind,
-- the owning tenant, and the creation instant. Moving the kind would break the
-- equivalence that ties a standing value to the procedural kind, and moving the
-- tenant would re-attribute stored content by an update.
--
-- Beyond that, the capture path may move standing and nothing else. That is the
-- confinement the requirement asks for, and stating it as the full list of the
-- other columns is what makes it total over the table as it stands rather than
-- over the columns a reader happened to think of.
--
-- The erasure path keeps the columns a surgical redaction rewrites: the body, its
-- digest, the revision counter, the update instant, the redaction instant, the
-- embedding state that rewrite invalidates, and the expiry. Redaction is a
-- governed operation that leaves a disposition record behind, so it is evidenced
-- rather than confined.
--
-- molt:own-transaction
DROP TRIGGER IF EXISTS molt_derived_update_scope_guard ON derived_artifact;

-- molt:own-transaction
CREATE OR REPLACE FUNCTION molt_derived_update_scope() RETURNS TRIGGER
    LANGUAGE PLpgSQL AS '
DECLARE
    unrestricted BOOL := pg_has_role(current_user, ''admin'', ''MEMBER'');
    may_rewrite_content BOOL := pg_has_role(current_user, ''molt_eraser'', ''MEMBER'');
BEGIN
    IF unrestricted THEN
        RETURN NEW;
    END IF;
    IF (NEW).id IS DISTINCT FROM (OLD).id
        OR (NEW).kind IS DISTINCT FROM (OLD).kind
        OR (NEW).owner_client_id IS DISTINCT FROM (OLD).owner_client_id
        OR (NEW).created_at IS DISTINCT FROM (OLD).created_at THEN
        RAISE EXCEPTION ''the identity and tenancy columns of a derived artifact are not writable'';
    END IF;
    IF NOT may_rewrite_content AND (
        (NEW).body IS DISTINCT FROM (OLD).body
        OR (NEW).content_digest IS DISTINCT FROM (OLD).content_digest
        OR (NEW).derivation_method IS DISTINCT FROM (OLD).derivation_method
        OR (NEW).revision IS DISTINCT FROM (OLD).revision
        OR (NEW).updated_at IS DISTINCT FROM (OLD).updated_at
        OR (NEW).redacted_at IS DISTINCT FROM (OLD).redacted_at
        OR (NEW).embedding_state IS DISTINCT FROM (OLD).embedding_state
        OR (NEW).expires_at IS DISTINCT FROM (OLD).expires_at) THEN
        RAISE EXCEPTION ''an update of a derived artifact by this role is confined to the standing column'';
    END IF;
    RETURN NEW;
END
';

-- molt:own-transaction
CREATE TRIGGER molt_derived_update_scope_guard
    BEFORE UPDATE ON derived_artifact
    FOR EACH ROW EXECUTE FUNCTION molt_derived_update_scope();

-- The lease guard. A lease may be renewed and it may be closed: its expiry moves,
-- its renewal instant moves, and the closing pair naming the successor is set
-- once. Its owner, its fencing generation, its tenant, its acquisition instant,
-- and the key that identifies the granting attempt are immutable once granted,
-- and that immutability is what the fence rests on. A generation that could be
-- restated would order nothing, and an owner that could be restated would let a
-- worker inherit another worker's ownership by an update rather than by the
-- ordered supersession the lease protocol requires.
--
-- molt:own-transaction
DROP TRIGGER IF EXISTS molt_erasure_lease_update_scope_guard ON erasure_lease;

-- molt:own-transaction
CREATE OR REPLACE FUNCTION molt_erasure_lease_update_scope() RETURNS TRIGGER
    LANGUAGE PLpgSQL AS '
BEGIN
    IF pg_has_role(current_user, ''admin'', ''MEMBER'') THEN
        RETURN NEW;
    END IF;
    IF (NEW).id IS DISTINCT FROM (OLD).id
        OR (NEW).client_id IS DISTINCT FROM (OLD).client_id
        OR (NEW).owner IS DISTINCT FROM (OLD).owner
        OR (NEW).generation IS DISTINCT FROM (OLD).generation
        OR (NEW).idempotency_key IS DISTINCT FROM (OLD).idempotency_key
        OR (NEW).acquired_at IS DISTINCT FROM (OLD).acquired_at THEN
        RAISE EXCEPTION ''the ownership columns of a lease are immutable once granted'';
    END IF;
    RETURN NEW;
END
';

-- molt:own-transaction
CREATE TRIGGER molt_erasure_lease_update_scope_guard
    BEFORE UPDATE ON erasure_lease
    FOR EACH ROW EXECUTE FUNCTION molt_erasure_lease_update_scope();
