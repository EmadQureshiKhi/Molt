-- Row-Level TTL on the three content tables, so retention is enforced by the
-- cluster itself.
--
-- The claim this migration makes good on is narrow and worth stating exactly:
-- expiry depends on no scheduled process outside the database. There is no cron
-- entry on a host, no scheduled function, no operator who has to remember. The
-- cluster holds the schedule, the cluster runs the job, and the rows leave. If
-- every process this project ships were stopped forever, expired content would
-- still be deleted.
--
-- Three shapes here are load-bearing.
--
-- The expiration is an expression over the row's own column rather than a fixed
-- interval after insertion. A fixed interval would force one retention period on
-- every row in the table, and retention varies per Jurisdiction and therefore per
-- Client and therefore per row. The write path sets the column to the write
-- instant plus the interval of the owning Client's Jurisdiction, and the TTL job
-- reads whatever that column holds. Retention policy is consequently data, and a
-- Jurisdiction with a different period needs no schema change.
--
-- The job runs daily. Content in these tables is the record an auditor reads, so
-- an expiry that lands within a day of its due instant is a comfortable
-- tolerance, and a rarer job is a cheaper one. The working tier is configured
-- separately and much more aggressively, because disposability there has to be
-- observable rather than merely promised; the checkpoint tables get no TTL at
-- all, because a checkpoint's whole value is outliving the rows it commits to.
--
-- The delete batch size is deliberately small. The TTL job competes with
-- foreground traffic for the same ranges, and a large batch would take a bite big
-- enough to be felt by a capture request or a recall query. A small batch trades
-- a longer job for a job nobody notices, and it also keeps the job's request-unit
-- consumption inside the cost ceiling this deployment is held to.
--
-- Every statement is re-runnable: re-declaring the same storage parameters on a
-- table that already carries them leaves the configuration exactly as it was.

ALTER TABLE ledger SET (
    ttl_expiration_expression = 'expires_at',
    ttl_job_cron = '@daily',
    ttl_delete_batch_size = 500
);

ALTER TABLE derived_artifact SET (
    ttl_expiration_expression = 'expires_at',
    ttl_job_cron = '@daily',
    ttl_delete_batch_size = 500
);

ALTER TABLE embedding SET (
    ttl_expiration_expression = 'expires_at',
    ttl_job_cron = '@daily',
    ttl_delete_batch_size = 500
);
