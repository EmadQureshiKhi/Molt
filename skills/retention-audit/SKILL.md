---
name: retention-audit
description: >-
  Audits database-enforced retention per client against a live CockroachDB
  cluster, reporting each client's jurisdiction, its configured retention
  interval, the count of artifacts expiring within the next seven days, and the
  count already expired. Use when checking that retention is configured as a
  jurisdiction requires, when a compliance reviewer asks what is about to
  expire, or when an expired count that should be falling is not falling.
license: MIT
compatibility: >-
  Requires the molt command-line interface on PATH and network reach to a
  CockroachDB cluster through the read-only reader role. No MCP server and no
  model provider credential is needed, because the report is a query.
allowed-tools: >-
  Read Bash(./scripts/retention_audit.sh:*)
metadata:
  molt-entry-point: scripts/retention_audit.sh
  molt-inputs: client-slug
  molt-outputs: >-
    client_slug, jurisdiction, retention_interval, expiring_within_seven_days,
    already_expired
  molt-behavior: >-
    Calls the retention report path, which returns one row per client naming
    that client's jurisdiction, the retention interval configured for that
    jurisdiction, the count of artifacts whose expiry timestamp falls within the
    next seven days, and the count whose expiry timestamp has already passed.
    Reads the configuration and the counts and changes neither: the report
    configures no row-level expiry, sets no interval, and removes no expired
    row, because expiry is enforced by the cluster itself rather than by the
    caller of this skill. Emits one JSON object per invocation on standard
    output and returns a non-zero status only when the report could not be
    produced.
  molt-operations: cli:retention
  molt-effect: read_only
  molt-database-role: reader
---

# Audit retention status per client

Retention here is enforced by the database: each artifact carries an expiry
timestamp equal to its write timestamp plus its client's jurisdiction interval,
and the cluster expires the row itself with no process outside it. That design
removes the operator from the deletion path, which means the only way to know
retention is working is to read what the cluster holds. This skill reads it.

## Behavior

The entry point calls the retention report path and prints one JSON object per
invocation on standard output. The report carries one entry per audited client:

| Field | Meaning |
|---|---|
| Jurisdiction | The jurisdiction configured for that client |
| Interval | The retention interval that jurisdiction carries |
| Expiring | The count of artifacts whose expiry falls within the next seven days |
| Expired | The count of artifacts whose expiry has already passed |

An already-expired count above zero is not automatically a finding. The cluster
expires rows on its own schedule rather than at the instant an expiry passes, so
a small expired count on a live cluster is the normal picture between expiry
passes. A count that grows across successive audits without falling is the
finding, because it says expiry is configured but not running.

Nothing is mutated. The report configures no expiry parameter, sets no interval,
and removes no expired row. Configuring expiry is a migration path, and this
skill neither invokes nor names it.

## Inputs

| Input | Required | Meaning |
|---|---|---|
| `client-slug` | no, repeatable | Slug of a client to audit; naming none audits every client the report covers |

## Outputs

Standard output carries one JSON object per invocation. Standard error carries
narration only.

| Output | Meaning |
|---|---|
| `client_slug` | Slug of the audited client |
| `jurisdiction` | The jurisdiction configured for that client |
| `retention_interval` | The retention interval that jurisdiction carries |
| `expiring_within_seven_days` | Count of artifacts whose expiry falls within the next seven days |
| `already_expired` | Count of artifacts whose expiry has already passed |

## Read-only posture

| Declared operation | Kind |
|---|---|
| `cli:retention` | Retention report path, reader role |

The report path connects with the reader role, which holds `SELECT` and no
`INSERT`, `UPDATE`, or `DELETE`. The entry point sets the role selector
explicitly rather than inheriting whichever role the surrounding environment
happens to name, so the posture does not depend on how the skill was invoked.

## Steps

1. Ask the caller which clients to audit, or audit every covered client when the
   caller names none.
2. Run `scripts/retention_audit.sh` with those arguments.
3. Report per client the jurisdiction, the interval, the expiring count, and the
   expired count together. An interval without its counts says nothing about
   whether expiry is working, and a count without its interval cannot be judged.
4. Flag a client whose configured interval differs from what its jurisdiction
   requires, which is a configuration finding rather than an expiry finding.
5. Where an expired count is high, say what would confirm the diagnosis: a
   second audit later, showing whether the count falls.
