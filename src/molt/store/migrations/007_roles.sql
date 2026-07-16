-- Roles and privileges: the four least-privilege roles every component connects
-- with, and the writable-column guards that confine what each of them may
-- restate.
--
-- This migration is where the governance claims stop being application
-- discipline and become database enforcement. Four of them in particular are
-- true here or nowhere.
--
-- No role holds UPDATE on the ledger. The episodic record is append-only, and
-- that is the whole basis of the hash chain's tamper evidence: a chain whose
-- rows can be edited in place commits to nothing. Rows leave the ledger by an
-- authorised erasure or by row-level expiry, never by revision, and the closing
-- revocation says so explicitly for every role rather than relying on the grant
-- lists above it having omitted it.
--
-- The reader role holds SELECT and nothing else, on anything. It is what the
-- independent certificate verifier, the sensitivity analyser, the write-stream
-- read path, the memory-protocol server, and the auditor views connect with, so
-- "a third party cannot mutate anything while verifying" is a privilege fact
-- rather than a promise.
--
-- The eraser role holds DELETE on the ledger and no UPDATE on it. A ledger row
-- can be removed by an authorised erasure and never edited. It is granted no
-- DELETE at all on the evidence tables, so the later revocation that makes the
-- absence explicit removes a privilege that was never held rather than one this
-- migration handed out.
--
-- Attribution is closable and not restatable. The detection method, the
-- confidence, the artifact, and the tenant of a stored attribution version are
-- unwritable after insert, which is what makes "when did you first hold this"
-- answerable against a history rather than against an editable row.
--
-- On the mechanism for that last one. The privilege model this database offers
-- has no column list: GRANT admits a table and a privilege and nothing finer,
-- and a view narrowed to the writable columns is not updatable either. Column
-- scoping is therefore expressed as a guard that runs before every update and
-- refuses a statement that changes a column the acting role may not change. The
-- enforcement point is still the database rather than the writing statement,
-- which is what the requirement asks for; only its shape differs from a column
-- list. Each guard names the columns that are immutable rather than the columns
-- that are writable, so a column added by a later migration is writable unless
-- a later guard protects it, and the two attribution closure columns a later
-- migration adds are writable by construction.
--
-- The admin path is exempt from both guards. A database administrator can
-- already drop a table, so a guard that pretended otherwise would be theatre;
-- the answer to a hostile administrator is the externally signed checkpoint, not
-- a trigger. Exempting admin is also what keeps schema work, seeding, and the
-- test fixtures able to write a row wholesale.
--
-- Scope of the grants. This migration grants only on the tables the first
-- migration generation creates. Every table the second generation adds carries
-- its grants in the migration that closes that generation, deliberately: an
-- applied migration is never edited, so folding a later table's grant back into
-- this file would change this file's recorded digest and the runner would refuse
-- to run at all. Two consequences are visible below. The writer's read of the
-- erasure lease is absent because the lease table does not exist yet, and the
-- guard on the session table names no column the second generation adds.
--
-- Every statement here is re-runnable. Role creation is guarded, a grant and a
-- revocation are both re-issuable with no effect the second time, and both a
-- guard function and a guard trigger are replaced rather than added. That
-- redundancy is deliberate: the runner already skips a recorded version, and
-- this file would also survive a second full application. The replacing form is
-- guard trigger is removed by name before it is created, which is what makes the
-- pair re-runnable given that this database refuses to replace a definition a
-- live trigger points at. Both attaching and removing a trigger are served only
-- by the newer schema changer, and that changer is reached only by a statement
-- that is a transaction by itself, so each statement of each guard carries the
-- marker that asks the runner for exactly that. The marker is written immediately
-- above the statement it applies to and nowhere else in this file.

CREATE ROLE IF NOT EXISTS molt_writer;
CREATE ROLE IF NOT EXISTS molt_eraser;
CREATE ROLE IF NOT EXISTS molt_reader;
CREATE ROLE IF NOT EXISTS molt_watcher;

-- The capture and ingest path. It appends content and reads what it needs to
-- attribute and to answer a recall, and it may move a session's counters and
-- close a session. It restates nothing.
GRANT SELECT, INSERT ON TABLE ledger, session, derived_artifact, lineage_edge,
    client_binding, embedding TO molt_writer;
GRANT SELECT ON TABLE client, erasure_run, policy_rule, approval_queue TO molt_writer;
GRANT UPDATE ON TABLE session TO molt_writer;

-- The only mutation the writer may make to attribution is closing a version.
-- The guard below is what confines it to that.
GRANT UPDATE ON TABLE client_binding TO molt_writer;

-- The erasure path. It removes memory content and writes the evidence of having
-- removed it. It holds DELETE on the ledger and no UPDATE on it, and no DELETE
-- on any evidence table.
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE derived_artifact, lineage_edge,
    client_binding, embedding TO molt_eraser;
GRANT SELECT, DELETE ON TABLE ledger TO molt_eraser;
GRANT SELECT, INSERT, UPDATE ON TABLE erasure_request, erasure_run, erasure_candidate,
    residue_candidate, disposition, run_session, backup_record, erasure_certificate,
    audit_log_snapshot TO molt_eraser;
GRANT SELECT ON TABLE session, client, capability TO molt_eraser;
GRANT UPDATE ON TABLE session TO molt_eraser;

-- The read-only path. SELECT and nothing else, on anything.
GRANT SELECT ON TABLE client, session, ledger, derived_artifact, lineage_edge,
    client_binding, embedding, erasure_run, disposition, residue_candidate,
    erasure_certificate, capability TO molt_reader;

-- The write-stream watcher. It reads the append stream, records what a rule
-- matched, queues an approval, and stops an agent. It writes no memory content
-- and holds nothing beyond SELECT on the ledger.
GRANT SELECT ON TABLE ledger, session, client, policy_rule, policy_match,
    approval_queue TO molt_watcher;
GRANT INSERT ON TABLE policy_match, approval_queue TO molt_watcher;
GRANT UPDATE ON TABLE session TO molt_watcher;
-- Its own resume point, which is the one row the polling fallback owns. The
-- watermark is watcher state rather than memory content, and a watcher that
-- cannot persist a resume point re-reads the stream from the beginning.
GRANT SELECT, INSERT, UPDATE ON TABLE watcher_watermark TO molt_watcher;

-- The append-only guarantee, stated rather than merely omitted. No grant above
-- confers UPDATE on the ledger, and this revokes it from every role so the
-- absence is a recorded decision.
REVOKE UPDATE ON TABLE ledger FROM molt_writer, molt_eraser, molt_reader, molt_watcher;

-- The session guard. Tenancy and lineage are unwritable by every role: a
-- compromised capture credential cannot move a session to another tenant or
-- restate what spawned it. Beyond that, each role may write only the columns its
-- job needs. The writer moves counters and closes a session, the erasure path
-- closes a session, and the watcher raises the halt.
--
-- Each guard is written as a removal, a definition, and an attachment, in that
-- order, and all three carry the marker. The removal is what makes the trio
-- re-runnable: this database refuses to replace the definition a live attachment
-- points at, so the attachment comes off first. The marker is what lets any of
-- the three run at all, because both attaching and removing are served only by
-- the newer schema changer.
--
-- molt:own-transaction
DROP TRIGGER IF EXISTS molt_session_update_scope_guard ON session;

-- The body is given as a quoted literal rather than in dollar-quoted form,
-- because the runner splits a migration into statements on semicolons outside
-- quoted text and a dollar-quoted body would be split at every statement it
-- holds. A single quote inside the body is therefore doubled.
--
-- molt:own-transaction
CREATE OR REPLACE FUNCTION molt_session_update_scope() RETURNS TRIGGER
    LANGUAGE PLpgSQL AS '
DECLARE
    unrestricted BOOL := pg_has_role(current_user, ''admin'', ''MEMBER'');
    may_write_counters BOOL := pg_has_role(current_user, ''molt_writer'', ''MEMBER'');
    may_write_terminal BOOL := pg_has_role(current_user, ''molt_writer'', ''MEMBER'')
        OR pg_has_role(current_user, ''molt_eraser'', ''MEMBER'');
    may_write_halt BOOL := pg_has_role(current_user, ''molt_watcher'', ''MEMBER'');
BEGIN
    IF unrestricted THEN
        RETURN NEW;
    END IF;
    IF (NEW).id IS DISTINCT FROM (OLD).id
        OR (NEW).client_id IS DISTINCT FROM (OLD).client_id
        OR (NEW).agent_cli IS DISTINCT FROM (OLD).agent_cli
        OR (NEW).machine_id IS DISTINCT FROM (OLD).machine_id
        OR (NEW).team_id IS DISTINCT FROM (OLD).team_id
        OR (NEW).attribution IS DISTINCT FROM (OLD).attribution
        OR (NEW).workspace_path IS DISTINCT FROM (OLD).workspace_path
        OR (NEW).started_at IS DISTINCT FROM (OLD).started_at
        OR (NEW).parent_session_id IS DISTINCT FROM (OLD).parent_session_id
        OR (NEW).spawning_event_id IS DISTINCT FROM (OLD).spawning_event_id
        OR (NEW).depth IS DISTINCT FROM (OLD).depth THEN
        RAISE EXCEPTION ''the tenancy and lineage columns of a session are not writable'';
    END IF;
    IF NOT may_write_counters AND (
        (NEW).tool_call_count IS DISTINCT FROM (OLD).tool_call_count
        OR (NEW).model_request_count IS DISTINCT FROM (OLD).model_request_count
        OR (NEW).error_count IS DISTINCT FROM (OLD).error_count
        OR (NEW).token_count IS DISTINCT FROM (OLD).token_count
        OR (NEW).cost_usd IS DISTINCT FROM (OLD).cost_usd) THEN
        RAISE EXCEPTION ''the session counters are not writable by this role'';
    END IF;
    IF NOT may_write_terminal AND (
        (NEW).ended_at IS DISTINCT FROM (OLD).ended_at
        OR (NEW).outcome IS DISTINCT FROM (OLD).outcome) THEN
        RAISE EXCEPTION ''the terminal columns of a session are not writable by this role'';
    END IF;
    IF NOT may_write_halt AND (
        (NEW).halted IS DISTINCT FROM (OLD).halted
        OR (NEW).halted_at IS DISTINCT FROM (OLD).halted_at
        OR (NEW).halt_reason IS DISTINCT FROM (OLD).halt_reason
        OR (NEW).halt_rule_id IS DISTINCT FROM (OLD).halt_rule_id) THEN
        RAISE EXCEPTION ''the halt columns of a session are not writable by this role'';
    END IF;
    RETURN NEW;
END
';

-- molt:own-transaction
CREATE TRIGGER molt_session_update_scope_guard BEFORE UPDATE ON session
    FOR EACH ROW EXECUTE FUNCTION molt_session_update_scope();

-- The attribution guard. A stored attribution version is an immutable statement:
-- the artifact it is about, the tenant it names, the method that detected it, the
-- confidence it carries, and when it was detected are all unwritable once
-- written. Closing a version is the only mutation any role may make, and the
-- columns that closure writes arrive in a later migration and are unprotected
-- here by design. The guard applies to the erasure path as well as to capture,
-- because immutability that one role can step around is not immutability.
--
-- molt:own-transaction
DROP TRIGGER IF EXISTS molt_client_binding_update_scope_guard ON client_binding;

-- molt:own-transaction
CREATE OR REPLACE FUNCTION molt_client_binding_update_scope() RETURNS TRIGGER
    LANGUAGE PLpgSQL AS '
BEGIN
    IF pg_has_role(current_user, ''admin'', ''MEMBER'') THEN
        RETURN NEW;
    END IF;
    IF (NEW).id IS DISTINCT FROM (OLD).id
        OR (NEW).artifact_id IS DISTINCT FROM (OLD).artifact_id
        OR (NEW).artifact_kind IS DISTINCT FROM (OLD).artifact_kind
        OR (NEW).client_id IS DISTINCT FROM (OLD).client_id
        OR (NEW).method IS DISTINCT FROM (OLD).method
        OR (NEW).confidence IS DISTINCT FROM (OLD).confidence
        OR (NEW).detected_at IS DISTINCT FROM (OLD).detected_at THEN
        RAISE EXCEPTION ''a stored attribution version is immutable and may only be closed'';
    END IF;
    RETURN NEW;
END
';

-- molt:own-transaction
CREATE TRIGGER molt_client_binding_update_scope_guard
    BEFORE UPDATE ON client_binding
    FOR EACH ROW EXECUTE FUNCTION molt_client_binding_update_scope();
