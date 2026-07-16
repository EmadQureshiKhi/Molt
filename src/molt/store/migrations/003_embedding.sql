-- Embeddings, the distributed vector index over them, and the capability record.
--
-- Four shapes here are load-bearing rather than incidental.
--
-- The vector column has one fixed width and no other. The provider selector
-- probes the configured embedding implementation at startup and refuses any
-- implementation reporting a different width before a single vector is written,
-- so the width is a schema fact rather than a per-row one. The dimension column
-- restates that fact and the check pins it, which means a row that disagrees
-- with the column type is refused twice over.
--
-- The provider name sits alongside the model identifier on every row, and the
-- uniqueness constraint spans the artifact, its kind, the provider, and the
-- model. A corpus embedded before a provider switch and re-embedded after it
-- therefore stays distinguishable row by row, and two providers offering a model
-- under the same name cannot silently collide into one row.
--
-- The distributed vector index is what makes semantic recall and residue
-- detection index-served rather than a full scan. Its operator class orders by
-- L2 distance, which is why every vector is scaled to unit norm before it is
-- written: over unit vectors the L2 ordering and the cosine ordering are the
-- same ordering, so the thresholds stay expressed in cosine space while the
-- index does the work. That normalisation is a requirement of correctness here,
-- not a defensive habit.
--
-- The index statement is marked as permitted to fail. On a cluster tier that
-- offers no distributed vector index the statement is rejected, the outcome is
-- reported, and the run continues, because the fixed column width and the text
-- of every query are the same either way: without the index the same
-- nearest-neighbour statement is served by an exact scan bounded by the tenant
-- covering index and an explicit row cap. The capability row recording which of
-- the two happened is inserted by the caller that reads the reported outcome,
-- and not by this file: a statement permitted to fail is applied after the body
-- of its migration has already committed, so no statement in the body can
-- observe its result.

CREATE TABLE IF NOT EXISTS embedding (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Polymorphic across the artifact kinds that carry embeddable text, so no
    -- database reference is declared; the inserting statement writes the
    -- embedding in the same transaction as the artifact it represents.
    artifact_id   UUID NOT NULL,
    artifact_kind STRING NOT NULL,
    client_id     UUID NOT NULL REFERENCES client (id),
    -- The provider name and the model identifier are both recorded, because a
    -- model identifier alone does not say which service produced the vector.
    provider      STRING NOT NULL,
    model_id      STRING NOT NULL,
    dimension     INT NOT NULL DEFAULT 1024,
    -- Unit normalisation is asserted per row, so a vector that reached the table
    -- through a path which skipped the scaling step is identifiable rather than
    -- merely suspected.
    normalised    BOOL NOT NULL DEFAULT true,
    vec           VECTOR(1024) NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    CONSTRAINT embedding_kind_known CHECK (artifact_kind IN ('event', 'derived_artifact')),
    -- The width the column type already fixes, restated as a row-level refusal.
    CONSTRAINT embedding_dimension_fixed CHECK (dimension = 1024),
    -- One vector per artifact per provider-and-model pair. The provider belongs
    -- in the span: without it a re-embedding under a second provider would
    -- collide with the first rather than sit beside it.
    CONSTRAINT embedding_unique_per_model UNIQUE (artifact_id, artifact_kind, provider, model_id),
    -- The covering index that bounds the exact-scan fallback: the tenant
    -- restriction and the projected columns are both served from the index, so
    -- the fallback reads no row it does not return.
    INDEX embedding_by_client (client_id, artifact_kind, artifact_id),
    -- Serves the per-artifact lookup the erasure sweep and the disposition
    -- writer both perform.
    INDEX embedding_by_artifact (artifact_id)
);

-- The vector column is the whole of the index and its last column, which is what
-- the index shape admits: a trailing column of string type is refused here.
-- molt:permit-failure vector_index
CREATE VECTOR INDEX IF NOT EXISTS embedding_vec_idx ON embedding (vec);

-- One row per probed platform fact, read once at process start. Every branch a
-- component takes over a platform capability is driven by a row here rather than
-- by a cluster version string, so a capability that was probed and a capability
-- that was assumed are never confused. The row for the vector index is inserted
-- by the caller that reads the reported outcome of the statement above.
CREATE TABLE IF NOT EXISTS capability (
    name       STRING PRIMARY KEY,
    available  BOOL NOT NULL,
    detail     STRING NULL,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
