-- The structural diff summary a surgical redaction leaves on its disposition row.
--
-- A redaction's whole claim is that one tenant's content left a shared body and
-- every other tenant's content stayed. Before this migration the row carried the
-- digest either side of the rewrite, which proves the body changed and says
-- nothing about how much of it survived, so the comparison view had to either
-- re-read both bodies -- one of which no longer exists -- or assert nothing. After
-- it, the row carries the two counts the rewriter already computes, and the
-- comparison view is a query over stored evidence.
--
-- Three shapes here are load-bearing rather than incidental.
--
-- The counts are counts and never text. A stored diff, or a stored list of the
-- segments that went, would be a copy of the pre-redaction body under another
-- name, and the disposition table is precisely the place no body may land. Two
-- whole numbers carry the proportion a reader needs and carry no content at all.
--
-- Both columns are nullable, because two of the three dispositions summarise no
-- rewrite. A hard delete removed the body outright and a retention left it
-- untouched; neither performed a rewrite, so neither has segments to report, and a
-- zero there would be a claim that a rewrite dropped nothing rather than the fact
-- that no rewrite happened. Absence is the honest value, so the check admits it.
--
-- The non-negativity is asserted per column rather than over the pair. A count of
-- segments below zero describes nothing, and stating the bound on each column
-- separately means a row carrying one count and not the other is still checked on
-- the count it carries. Whether the pair is present together is the writing path's
-- business: every disposition of every path goes through one insert, so the pair
-- travels as one value or as nothing.
--
-- One note on shape, forced by the platform rather than chosen. A constraint
-- cannot be removed and re-added under one name inside the same transaction that
-- added the column it reads, because that column is not yet visible to the
-- constraint builder. The column additions therefore stay in the migration's own
-- transaction and each constraint statement carries the marker asking for a
-- transaction of its own, which the runner applies after the body has committed.
-- The drop-then-add pairing is what keeps the file re-runnable, since a constraint
-- addition admits no guard of its own.
--
-- No privilege is granted here. The disposition table's privileges are already
-- carried by the grants migration, and a column added to an existing table is
-- covered by the table-level grant that migration states.

ALTER TABLE disposition ADD COLUMN IF NOT EXISTS removed_segments INT NULL;

ALTER TABLE disposition ADD COLUMN IF NOT EXISTS retained_segments INT NULL;

-- Absence admitted, because a hard delete and a retention summarise no rewrite.
-- Present values are counts, so zero is admitted and nothing below it is.
--
-- molt:own-transaction
ALTER TABLE disposition DROP CONSTRAINT IF EXISTS disposition_removed_segments_counted;

-- molt:own-transaction
ALTER TABLE disposition ADD CONSTRAINT disposition_removed_segments_counted CHECK (
    removed_segments IS NULL OR removed_segments >= 0);

-- molt:own-transaction
ALTER TABLE disposition DROP CONSTRAINT IF EXISTS disposition_retained_segments_counted;

-- molt:own-transaction
ALTER TABLE disposition ADD CONSTRAINT disposition_retained_segments_counted CHECK (
    retained_segments IS NULL OR retained_segments >= 0);
