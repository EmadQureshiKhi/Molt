-- Derived artifacts, the lineage graph over them, and tenant attribution.
--
-- Three shapes here are load-bearing rather than incidental.
--
-- A lineage parent is polymorphic across the three artifact kinds an artifact
-- can be derived from, so the parent column carries no database reference. The
-- insert statement enforces existence by joining the artifact_ref view, which
-- returns no row for a parent that does not exist and so makes the write fail.
-- The alternative shape, three nullable typed columns each with its own
-- reference, was rejected: the recursive descendant query would then need a
-- three-way coalesce in its join predicate, and that predicate cannot be served
-- by the parent traversal index.
--
-- Both traversal directions get their own index. Descendant closure walks
-- parent to child and ancestor closure walks child to parent, and each direction
-- is a separate recursive query whose join must be index-served for the closure
-- to stay inside its time bound on a graph of a hundred thousand edges.
--
-- The view spans exactly the three kinds that may be a lineage parent, and
-- omitting the others is the enforcement rather than a documentation choice.
-- Neither an embedding nor a disposable working row appears in it, so neither
-- can pass the existence join, so neither can become a lineage parent.

CREATE TABLE IF NOT EXISTS derived_artifact (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind              STRING NOT NULL,
    owner_client_id   UUID NOT NULL REFERENCES client (id),
    body              STRING NOT NULL,
    content_digest    STRING NOT NULL,
    derivation_method STRING NOT NULL,
    revision          INT NOT NULL DEFAULT 1,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    redacted_at       TIMESTAMPTZ NULL,
    embedding_state   STRING NOT NULL DEFAULT 'pending',
    expires_at        TIMESTAMPTZ NOT NULL,
    CONSTRAINT derived_kind_known CHECK (kind IN (
        'summary', 'behavioral_baseline', 'learned_procedure')),
    CONSTRAINT derived_embedding_state_known CHECK (embedding_state IN (
        'not_required', 'pending', 'embedded', 'failed')),
    CONSTRAINT derived_digest_hex CHECK (length(content_digest) = 64),
    -- A body is revisable, so a revision counter starts at the first version and
    -- only ever moves forward.
    CONSTRAINT derived_revision_positive CHECK (revision >= 1),
    INDEX derived_by_client (owner_client_id, created_at DESC),
    INDEX derived_by_kind (kind, created_at DESC),
    INDEX derived_pending_embedding (created_at ASC) WHERE embedding_state = 'pending'
);

CREATE TABLE IF NOT EXISTS lineage_edge (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Removing a derived artifact removes the edges that named it as a child,
    -- because those edges are recomputable from the derivation rather than
    -- evidence in their own right.
    child_id          UUID NOT NULL REFERENCES derived_artifact (id) ON DELETE CASCADE,
    -- Polymorphic across the three parent kinds, so no reference is declared
    -- here; the inserting statement joins the artifact_ref view instead.
    parent_id         UUID NOT NULL,
    parent_kind       STRING NOT NULL,
    derivation_method STRING NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT lineage_parent_kind_known CHECK (parent_kind IN (
        'event', 'session', 'derived_artifact')),
    -- The shortest cycle a graph admits is an artifact deriving from itself, and
    -- that one is refused by the schema. Longer cycles are refused by the
    -- inserting statement, which checks reachability before it writes.
    CONSTRAINT lineage_no_self_edge CHECK (child_id != parent_id),
    CONSTRAINT lineage_edge_unique UNIQUE (child_id, parent_id),
    -- One index per traversal direction: descendant closure walks parent to
    -- child, ancestor closure walks child to parent.
    INDEX lineage_by_parent (parent_id),
    INDEX lineage_by_child (child_id)
);

CREATE TABLE IF NOT EXISTS client_binding (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Polymorphic across every artifact kind that can hold tenant content,
    -- embeddings included, so no reference is declared here either.
    artifact_id   UUID NOT NULL,
    artifact_kind STRING NOT NULL,
    client_id     UUID NOT NULL REFERENCES client (id),
    method        STRING NOT NULL,
    confidence    FLOAT8 NOT NULL,
    detected_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT binding_kind_known CHECK (artifact_kind IN (
        'event', 'session', 'derived_artifact', 'embedding')),
    CONSTRAINT binding_method_known CHECK (method IN (
        'scope', 'inherited', 'marker', 'residue')),
    -- Confidence is a closed unit interval, both ends admitted.
    CONSTRAINT binding_confidence_range CHECK (confidence >= 0.0 AND confidence <= 1.0),
    -- The total uniqueness of the artifact and tenant pair, as first created. A
    -- later migration drops this constraint and puts a partial unique index over
    -- unsuperseded versions in its place, because attribution becomes an
    -- immutable version history at that point: a history necessarily holds
    -- several rows for one pair while admitting exactly one current among them,
    -- which a total constraint cannot express. The total form is created here so
    -- that the pair is unique from the first write onward rather than only from
    -- the migration that introduces the history.
    CONSTRAINT binding_unique_pair UNIQUE (artifact_id, client_id),
    INDEX binding_by_client (client_id, artifact_kind, artifact_id),
    INDEX binding_by_artifact (artifact_id)
);

-- The set of artifacts a lineage edge may name as a parent, and the tenant each
-- one belongs to. Exactly three kinds appear. Embeddings are absent because an
-- embedding is a representation of an artifact rather than an artifact anything
-- is derived from, and disposable working rows are absent because nothing may
-- depend on a working row surviving. Both omissions are structural: a parent
-- that the view does not return cannot pass the existence join the inserting
-- statement performs.
CREATE VIEW IF NOT EXISTS artifact_ref AS
    SELECT id, 'event' AS kind, client_id FROM ledger
    UNION ALL
    SELECT id, 'session' AS kind, client_id FROM session
    UNION ALL
    SELECT id, 'derived_artifact' AS kind, owner_client_id AS client_id FROM derived_artifact;
