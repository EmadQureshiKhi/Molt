-- Attribution becomes a bitemporal version history rather than an editable row.
--
-- Before this migration a tenant binding was one mutable row holding a current
-- opinion. After it, a binding is an attribution version: an immutable statement
-- carrying a validity interval and an explicit successor. The question an auditor
-- actually asks -- when did you first attribute this artifact to my tenant, and
-- what has changed since -- is unanswerable against a row that gets overwritten
-- and answerable by construction against a history. That is the whole purpose of
-- the change: the record cannot be quietly restated.
--
-- Four shapes here are load-bearing.
--
-- The total uniqueness of the artifact and tenant pair is dropped and a partial
-- unique index over unsuperseded versions takes its place. This is the trick the
-- history turns on. A history necessarily holds many rows for one pair, one per
-- version, so a total constraint would refuse the second version outright. The
-- partial index constrains only the rows whose successor reference is null, so
-- many closed versions accumulate for a pair while exactly one stays current. The
-- two properties the governance claim needs -- a history that accumulates and a
-- single unambiguous current claim -- are therefore both facts the database
-- enforces rather than rules the writing code follows.
--
-- Closure is total. A version is either current in both the validity end and the
-- successor reference, or closed in both. A half-closed version is a hole in the
-- history: closed with no successor loses the thread of what replaced it, and a
-- successor with no validity end leaves two rows claiming the same instant. The
-- check admits neither, so no role can write either shape, including a role
-- closing a version legitimately.
--
-- The validity interval is ordered. An end that precedes its start describes no
-- interval, and the as-of query's containment predicate would then silently
-- return nothing for a timestamp inside the intended range. The check refuses it
-- at the write instead.
--
-- The as-of index stores the projection the as-of query reads. Answering "which
-- tenant did this artifact belong to at this instant" is a range over one
-- artifact's versions ordered by validity start, and the tenant, the detection
-- method, the confidence, and the successor reference are all carried in the
-- index itself. The query therefore needs no row fetch, which is what holds the
-- one-second bound for an artifact carrying a hundred versions or more: the cost
-- is the range scan alone rather than the range scan plus one lookup per version.
--
-- Three notes on shape, each forced by the platform rather than chosen.
--
-- The old uniqueness constraint is removed by dropping the index that backs it
-- rather than by naming it as a constraint. This database implements no
-- constraint-removal path for a uniqueness constraint and says so, directing the
-- caller to the backing index instead.
--
-- An index whose predicate or stored columns name a column added earlier in the
-- same transaction is refused, because that column is not yet visible to the
-- index builder. Both index creations therefore carry the marker asking for a
-- transaction of their own, and both run after the column additions have
-- committed.
--
-- A constraint cannot be removed and re-added under the same name inside one
-- transaction either, for the same reason: the removal is not yet visible when
-- the addition is checked. Every constraint statement below is consequently
-- marked as well. That is what keeps the whole file re-runnable, which matters
-- for the ledger's category constraint in particular: it is replaced rather than
-- edited where it was first written, because an applied migration is never
-- edited -- changing that file would change its recorded digest and the runner
-- would refuse to run at all. The supersession category is appended to the end of
-- the list so the constraint's order still matches the order the category
-- enumeration declares.
--
-- One note on what is deliberately absent. The two closure columns added here are
-- unprotected by the attribution update guard an earlier migration attached. That
-- guard names the immutable columns rather than the writable ones, so these two
-- are writable by construction, and closing a version is the only mutation any
-- non-admin role can make to a stored version.

ALTER TABLE client_binding ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE client_binding ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ NULL;
ALTER TABLE client_binding ADD COLUMN IF NOT EXISTS superseded_by UUID NULL;

-- The total constraint an earlier migration created, removed because a version
-- history cannot satisfy it. The cascade removes the constraint entry along with
-- the index. Nothing else depends on that index, so nothing else is carried away
-- with it: the successor reference added above points at the primary key rather
-- than at this pair.
DROP INDEX IF EXISTS client_binding@binding_unique_pair CASCADE;

-- The successor reference points at another version of the same table, so the
-- reference is declared separately and under a name of its own rather than inline
-- on the column addition. A guarded column addition whose clause carries an
-- inline reference adds the reference a second time when the column is already
-- present, which would leave a duplicate on a re-application; a named reference
-- can be removed by name first and so is genuinely re-runnable.
--
-- molt:own-transaction
ALTER TABLE client_binding DROP CONSTRAINT IF EXISTS binding_superseded_by_fkey;

-- molt:own-transaction
ALTER TABLE client_binding ADD CONSTRAINT binding_superseded_by_fkey
    FOREIGN KEY (superseded_by) REFERENCES client_binding (id);

-- Exactly one current version per artifact and tenant pair. A superseded version
-- carries a successor, so it falls outside this index's predicate and the history
-- accumulates without limit alongside the single current row.
--
-- molt:own-transaction
CREATE UNIQUE INDEX IF NOT EXISTS binding_current_unique
    ON client_binding (artifact_id, client_id)
    WHERE superseded_by IS NULL;

-- Closure is total: current in both columns, or closed in both.
--
-- molt:own-transaction
ALTER TABLE client_binding DROP CONSTRAINT IF EXISTS binding_closure_consistent;

-- molt:own-transaction
ALTER TABLE client_binding ADD CONSTRAINT binding_closure_consistent CHECK (
    (valid_to IS NULL AND superseded_by IS NULL)
    OR (valid_to IS NOT NULL AND superseded_by IS NOT NULL));

-- A validity end never precedes its validity start.
--
-- molt:own-transaction
ALTER TABLE client_binding DROP CONSTRAINT IF EXISTS binding_interval_ordered;

-- molt:own-transaction
ALTER TABLE client_binding ADD CONSTRAINT binding_interval_ordered CHECK (
    valid_to IS NULL OR valid_to >= valid_from);

-- Serves the as-of query: one artifact's versions in validity order, with the
-- projection stored so the interval containment filter and the projection both
-- read from the index and no row is fetched.
--
-- molt:own-transaction
CREATE INDEX IF NOT EXISTS binding_as_of
    ON client_binding (artifact_id, valid_from DESC, valid_to DESC)
    STORING (client_id, method, confidence, superseded_by);

-- The supersession event category, appended so that no attribution history change
-- is silent: the ledger carries an event naming the artifact, the tenant, the
-- superseded version, and the superseding version, in the same transaction as the
-- two writes, and so inherits the hash chain.
--
-- molt:own-transaction
ALTER TABLE ledger DROP CONSTRAINT IF EXISTS ledger_category_known;

-- molt:own-transaction
ALTER TABLE ledger ADD CONSTRAINT ledger_category_known CHECK (category IN (
    'session_start', 'session_end', 'user_prompt', 'assistant_response',
    'tool_call', 'tool_result', 'model_request', 'model_response',
    'file_read', 'file_write', 'shell_command', 'decision', 'error',
    'cost_record', 'recall', 'policy_halt', 'attribution_superseded'));
