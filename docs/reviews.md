# Schema and query reviews

Requirement 27.10 obliges the development process to use the CockroachDB Agent
Skills material for schema review and query review, and obliges this documentation
to record the reviews performed and the changes they produced. Requirement 39.8
obliges the record to stand alongside the three shipped skills rather than be
replaced by them. This is that record.

**What this document can and cannot honestly claim.** The tree evidences
review-driven *change*: migrations that re-create what earlier migrations left
permissive, statements restructured after a plan was read rather than assumed,
probe records standing where an assumption used to, and a note in a provisioning
script recording discrepancies its author declined to resolve unilaterally. The
tree does not evidence a review *session* as a distinct event — no transcript, no
review log, no sign-off exists here, and none is invented below. Where the review
guide's contribution cannot be separated from ordinary design work, that is said
rather than dressed up.

So each entry below names the artefact that carries the change. A finding whose only
evidence would be an assertion that someone once looked at something is not listed.

## Reviews at a glance

| # | Reviewed | Finding | Change | Evidence |
|---|---|---|---|---|
| 1 | Referential actions across the erasure and checkpoint tables | Inline foreign keys from the first migration generation carried `ON DELETE CASCADE` or the platform default, so deleting one `erasure_run` row would remove the record of what it touched | Ten references re-created with `ON DELETE RESTRICT`; evidence separated from recomputable derivation per table | `013_protection.sql`, [protection.md](protection.md) |
| 2 | Privileges held by the four roles | The eraser's absence of `DELETE` on nine evidence tables was unwritten, so a later grant could confer it unnoticed | Explicit revocations, plus `UPDATE`/`DELETE` on both checkpoint tables revoked from all four roles | `014_grants.sql`, [protection.md](protection.md) |
| 3 | Column-level write confinement | The cluster offers no column-scoped `UPDATE` grant and no updatable view narrowed to writable columns | Three confinements expressed as database-side trigger guards instead of grants | `014_grants.sql`, `.kiro/specs/molt/design.md` |
| 4 | Supersession as one statement | The cluster refuses multiple mutations of one table in a single statement unless all are inserts | Two ordered statements in one serialisable transaction; the two `superseded_by` foreign keys dropped | `013_protection.sql`, `src/molt/store/attribution.py` |
| 5 | The recall query plan | A predicate on any non-vector column takes the plan off the vector index, and tenancy admission is exactly such a predicate | Recall staged so the first stage carries the ordering alone; admission applied to the candidate pool | `src/molt/store/embeddings.py` |
| 6 | The measured plan, not the intended one | The optimiser chooses a scan until table statistics exist | The corpus is analysed and the plan read back before anything is timed | [platform.md](platform.md), `tests/perf/test_recall_latency.py` |
| 7 | Vector index insert cost | Insert cost rises with rows already present, because the index splits and rebalances partitions as it fills | The measurement corpus is loaded with the index dropped and the index built once afterwards | [platform.md](platform.md) |
| 8 | The reported operator class | The index orders by squared L2 distance while every threshold here is cosine | Unit normalisation at write time made load-bearing, and non-unit or wrong-width vectors refused before the statement is sent | `src/molt/store/embeddings.py`, [providers.md](providers.md) |
| 9 | The pending-embedding sweep | Reading the state column alone would return every Event ever embedded, on every pass, because no role may update the ledger | An existence test against `embedding` per branch, with the state column kept as the leading term | `src/molt/store/embeddings.py` |
| 10 | The garbage-collection horizon | Measured at 4500 seconds — shorter than the evidence lifetime of a certificate | Derived counts made primary; the historical read demoted to opportunistic corroboration | [platform.md](platform.md), `src/molt/store/historical.py` |
| 11 | Backup surface of the control plane | Listing and configuration are offered; on-demand creation is not | The self-managed path made primary, with the managed backup referenced as fallback | [platform.md](platform.md), `scripts/probe_capabilities.py` |

---

## 1. Deleting evidence was possible by accident

The first migration generation declared its foreign keys inline, and those
references carried `ON DELETE CASCADE` or the platform's default. Removing a single
`erasure_run` row would therefore have taken the candidate set, the dispositions,
the per-session terminal digests, the backup record, the certificate, and the
cluster audit snapshot with it — the whole record of what that run did.

Migration 013 re-creates every reference to an erasure request, an erasure run, an
erasure lease, or a ledger checkpoint with `ON DELETE RESTRICT`, and states the
division it draws: evidence refuses to be cascaded away, and a row that is a
recomputable function of something that survives cascades freely. The per-table
argument for each is in [protection.md](protection.md) and is not restated here.

The audit trail is the migration sequence itself. The permissive declarations in
002, 003, 004, and 011 are still readable where they were made, and 013 corrects
them in a file of its own rather than editing history, so what the review found is
recoverable from the tree rather than only from this document.

Coverage: `tests/integration/test_referential_restrict.py` and
`test_referential_cascade.py`.

## 2. An absence nobody wrote down

Migration 014 revokes `DELETE` from the eraser role on nine evidence tables. No
grant in either generation ever conferred it — migration 007 grants the eraser
`SELECT, INSERT, UPDATE` on those tables and stops — so the revocation removes a
privilege that was never held.

That is the finding rather than a redundancy: an unstated absence can be undone by a
later grant without anyone noticing, while a stated revocation cannot. The same pass
revoked `UPDATE` and `DELETE` on `ledger_checkpoint` and `checkpoint_session` from
all four roles, because a checkpoint any Molt principal could rewrite would commit
to nothing.

One gap in the other direction closed in the same file. The writer role performs the
erasure guard read against `erasure_lease`, but that table does not exist until
migration 009, so migration 007 could not grant on it. The design records that grant
as an obligation of migration 014 and records it as *not* delivered; the delivered
`014_grants.sql` carries `GRANT SELECT ON TABLE erasure_lease TO molt_writer`, so the
gap is closed and the design's statement of it is now stale. See the open findings.

Coverage: `tests/integration/test_privileges.py` and `test_privileges_amended.py`.

## 3. No column-scoped grant exists, so the guard moved into the database

`GRANT` on this cluster admits a table and a privilege and nothing finer, and the
cluster offers no updatable view narrowed to writable columns either. Three
confinements the design needs are therefore expressed as database-side trigger
guards in migration 014 rather than as grants: a session's tenancy and lineage
columns are unwritable by every non-administrative role, a stored attribution
version may be closed but never restated, and the writer's update of a derived
artifact is confined to the confidence column.

Two consequences of the platform came out of the same pass. The administrative path
is exempt from all three guards, deliberately — an administrator can already drop
the table, so a guard pretending otherwise would be theatre. And the cluster refuses
to replace a function definition a live trigger points at, so each guard is written
as a guarded drop, then a definition, then an attachment, each carrying the
`-- molt:own-transaction` marker, which is what makes the trio re-runnable.

Requirement 34.12 obliges this platform fact to be recorded; that obligation is
discharged here and in [protection.md](protection.md).

## 4. Supersession cannot be one statement

Closing the current version of a binding is an update and writing its successor is
an insert. Expressing both as one statement through chained common table
expressions is unavailable: the cluster refuses it outright, reporting that multiple
mutations of one table are unsupported unless they all use `INSERT`.

The delivered shape is two statements in a fixed order inside one serialisable
transaction, the close first and the insert second, with the successor's identifier
generated before either statement runs so the closing statement can name a row that
does not exist yet. That ordering has a schema consequence, and it is the reason two
foreign keys are deliberately absent: `client_binding.superseded_by` and
`erasure_lease.superseded_by` would each refuse the closing statement, and reversing
the order would leave two current rows, which the partial unique index refuses.
Migration 013 drops both constraints — added by 008 and 009 — and the integrity of
the value comes from the transaction committing both statements or neither.

Coverage: `tests/integration/test_attribution_supersession.py`,
`tests/concurrency/test_attribution_race.py`,
`tests/integration/test_lease_lifecycle.py`.

## 5. The tenancy predicate took recall off the vector index

The query-plan finding that changed the most code. On this cluster a predicate on
any column other than the vector takes the plan off the distributed vector index,
and the tenancy admission — an Embedding is visible only through an unsuperseded
attribution version naming a permitted client — is exactly such a predicate.

The two read paths diverged on the strength of it, which is the substance of the
change rather than a detail of it:

| Path | Shape | Why |
|---|---|---|
| Neighbour query, used by residue detection | Flat: tenancy term beside the ordering, answered by a bounded seek and a sort | Exactness matters more than latency, and a residue sweep is not on anyone's critical path |
| Recall page, used by an agent before it acts | Staged: the first stage carries the ordering expression and nothing else, bounded by a caller-sized candidate pool; admission, provenance, floor, and ranking read that stage's output | A predicate in a later stage cannot reach back and change how the first stage was planned |

The staging has a cost and it is stated in the module rather than hidden: a page
assembled from a pool is the nearest k among the pool rather than among the corpus,
so a caller permitted a small slice of a large corpus can be answered with fewer
than k results while more exist further down the ordering. The caller is told when
the pool saturated, so under-recall is visible. Nothing admitted is ever wrong,
because the admission term is the same text in both paths.

Coverage: `tests/unit/test_store_embedding_statements.py` asserts the two forms
carry one admission term, `tests/property/test_p16_recall_tenancy.py` and
`test_p17_recall_ordering.py` the tenancy and ordering properties.

## 6, 7, and 8. What the latency measurement had to be changed to mean

Three findings from the same performance pass, all recorded in
[platform.md](platform.md) beside the figures they qualify.

An index-served measurement means nothing if the optimiser quietly chose a scan, and
on this cluster it will choose a scan until table statistics exist. So the corpus is
analysed before anything is timed and the plan for the store's own recall statement
— prefixed, not rebuilt — is read back and confirmed to name the index.

Building a hundred-thousand-row corpus with the index live is not merely slow: the
index splits and rebalances its partitions as it fills, so insert cost rises with
the rows already present. The corpus is therefore loaded with the index dropped and
the index built once afterwards, which weakens nothing about the figures, because
what is measured is query latency against a fully built index.

And the cluster reports the operator class it created, not the one the code asked
for. It orders by squared L2 distance while every threshold in this design is a
cosine distance. The two agree on unit vectors and disagree otherwise, so
normalisation at write time is load-bearing rather than defensive, and a vector of
the wrong width or the wrong length is refused before the statement is sent. The row
carries the width and the unit-norm assertion as stored columns, so a vector that
arrived by some other path is identifiable from the row.

## 9. The sweep could not trust the column it selected on

No role holds `UPDATE` on `ledger`, so an Event's embedding-state column is fixed by
the statement that appended it and stays `pending` for the life of the row, whatever
vector later landed. A sweep reading that column alone would therefore return every
Event that has ever been embedded, on every pass — a provider call per already
embedded Event in every fresh container — and the unembedded-coverage count
Requirement 10.9 asks a certificate to report would name artifacts that are fully
embedded.

The remedy considered and rejected was the privilege one: exempting the state column
from the ledger's `UPDATE` revocation. That would make a ledger row editable in
place, and the append-only ledger is what the hash chain's tamper evidence rests on.

So the change is in the statement. Each branch of the sweep carries an existence
test against `embedding` for that artifact and that kind, with the state column kept
as the leading term so the partial index over the pending rows still selects the
rows and still supplies the ascending order; the test resolves as an anti
lookup-join into the uniqueness index, which already leads on exactly that pair, so
no new index was needed.

Coverage: `tests/integration/test_pending_sweep_vector.py`.

## 10 and 11. Two probe results that changed which path is primary

The garbage-collection horizon on the delivered configuration measures 4500 seconds,
far shorter than typical defaults and shorter than the evidence lifetime of an
erasure certificate. A certificate's counts are therefore *derived* from the ledger
and the recorded dispositions, and the point-in-time read of the cluster's own
history is attempted only as corroboration, only when both instants fall inside the
horizon, and recorded as skipped otherwise. The horizon parse is anchored so a
fractional value reads as no reading at all rather than being truncated into a
plausible one.

The control plane offers backup listing and backup configuration and offers no
on-demand backup creation, which was probed rather than assumed. The self-managed
path is therefore primary — `BACKUP INTO` an operator-owned target, issued before
the first mutation of a run — and referencing the most recent managed backup by
identifier and instant is the fallback, with the recorded backup path distinguishing
taken from referenced.

Coverage: `tests/integration/test_capability_probes.py`,
`tests/integration/test_historical_horizon.py`.

---

## Open findings

Several findings are genuinely open. Two of the four carried forward from earlier
passes turn out to be closed in the delivered tree, and they are recorded that way
rather than left standing, because a stale open finding costs a reviewer the same
attention as a real one.

| # | Finding | Status |
|---|---|---|
| A | Three discrepancies between `provision_roles.sh` and the requirements | Open — decision belongs to the owner of that script |
| B | The tool server's HTTP transport authenticates no caller | Open by acceptance, and reported by a deliberately failing test |
| C | Three of the seven named threats are accepted in part | Open by acceptance |
| D | The design's statement that migration 014 lacks the writer grant on `erasure_lease` | Stale — the grant is delivered |
| E | The console runs as the eraser role while the sensitivity analyser requires the reader role | Closed in the delivered template; a residue remains for an unprovisioned deployment |
| F | Live certificate verification and the public key source | Wired; the route's own docstring is stale, and no certificate has been verified end to end |

### A. Three provisioning discrepancies

A `HANDOVER NOTE` above `provision_auditor()` in `scripts/provision_roles.sh`
records three places where that function and the requirements do not line up.
Nothing in the script was changed on the strength of them, and
[auditor.md](auditor.md) documents the narrower behaviour the script actually has,
so the guide is accurate either way.

1. **No named as-of-attribution view.** Requirement 43.11 has the auditor gateway
   expose the as-of query of 43.4 through the read-only view set. Three views are
   created and none is that one; the obligation is met only because the binding view
   happens to project the validity columns, leaving the as-of read as a predicate the
   auditor writes. If 43.11 means a named view, the function is one `CREATE VIEW`
   short.
2. **No control-plane service account per auditor.** Requirement 24.4 asks for one
   per auditor. Service accounts are created for the four service roles only; an
   auditor gets a database login with an expiry. The expiry obligation is satisfied
   and the control-plane account is not created.
3. **`molt_reader` does not back the auditor views.** Migration 007 and the glossary
   both say the reader role is what those views connect with, but `SELECT` is granted
   directly to the per-auditor login and `molt_reader` is never granted to it. The
   reach is narrower rather than wider, which is arguably the better outcome, but
   three statements of one design disagree and one of them should move.

### B. The tool transport authenticates no caller

`HTTP_AUTHENTICATION_POSTURE` reads *unauthenticated; network isolation is the only
control*, the health route reports it verbatim, and
`tests/security/test_route_authentication.py` reports the transport as
unauthenticated as a failing check with no exemption, no allowlist entry, and no
expected-failure marker. The configuration surface declares no credential for the
transport and the server invents none. Exposing it to a reachable network would
expose fleet memory to whoever reaches it and would breach Requirement 30.5. The
threat model records it as accepted; see [threat-model.md](threat-model.md) and
[mcp.md](mcp.md).

### C. Three threats accepted in part

Ledger tampering, ingress replay, and prompt injection into an adjudication prompt
each have a mechanism that narrows the attack without removing it: tamper evidence
rather than tamper proofing, a replayable window bounded by the maximum request age
rather than closed, and a validated provider response rather than an uninfluenceable
one. Each residue is named in [threat-model.md](threat-model.md) with the reason it
was not closed. They are listed here because a partial acceptance is a standing
finding, not a resolved one.

### D. A stale statement in the design

The design records `GRANT SELECT ON TABLE erasure_lease TO molt_writer` as an
obligation of migration 014 and states that, as delivered, migration 014 does not
carry it. It does. The finding is now a documentation drift in
`.kiro/specs/molt/design.md` rather than a missing grant, and the migration is the
authority.

### E. The console's database role

The console function holds the eraser role, because the erasure console runs
erasures from the same function, while the sensitivity analyser insists on a
connection authenticating as the read-only role so that its no-mutation claim is a
privilege fact rather than a promise. `Console.reader_store()` resolves that by
opening a second read-only connection from the parameter named by
`MOLT_READER_DSN_PARAM`, refusing any connection that authenticates as something
wider, and raising `ReaderRoleUnavailableError` when no such parameter is
configured — which the sensitivity route renders as an unavailable analysis rather
than running it under a role that can write.

The delivered configuration provisions it: `infra/templates/console.yaml` declares
`MOLT_READER_DSN_PARAM` under the reader connection's parameter path, and
`provision_roles.sh` writes a connection string there for each of the four service
roles. So the refusal path stands only for a deployment that skips role
provisioning or overrides that variable, and the residue is that such a deployment
loses one grid rather than failing loudly at startup.

Coverage: `tests/unit/test_console_reader_role.py`, including an assertion that the
refusal names the parameter to provision.

### F. Certificate verification and the public key source

Recorded in an earlier pass as unwired. It is wired now.
`src/molt/attest/keys.py` carries `KmsKeys`, `StoredPublicKey`, and
`public_key_source`, and `src/molt/console/routes/certificates.py::_key_source()`
resolves through `public_key_source` against the console's configuration surface,
raising only where that surface names no signing key at all — which is reported as a
missing component rather than as a verification outcome, because reporting an
unprovisioned deployment as *failed* would libel a valid certificate.

Two things remain. The module docstring of that route still says this build carries
no client for the key service, which is no longer true and should follow the code.
And no certificate has been assembled, signed, and verified end to end: `tests/e2e/`
holds no test, so the path is covered in parts — `tests/unit/test_key_service.py`,
`tests/integration/test_certificate_assembly.py`,
`tests/integration/test_checkpoint_verification.py` — and not as a run.

## What was left out

Two things a document like this is tempted to include are absent deliberately.

There is no narrative of review sessions, because the tree records outcomes and not
occasions. Every entry above is anchored to a migration, a statement, a probe
result, a note in a script, or a test, and a finding that could only be supported by
an assertion that a review happened is not listed.

And no review is claimed for the areas the guide would plausibly have covered but
where the tree shows no resulting change. Silence there means no evidence, not a
clean bill.

## Related documents

- [protection.md](protection.md) — the per-table argument behind findings 1 and 2.
- [platform.md](platform.md) — the probed facts behind findings 6 through 11, with
  the measured recall figures.
- [threat-model.md](threat-model.md) — the seven threats and the statuses behind
  open findings B and C.
- [auditor.md](auditor.md) — the auditor surface as the script actually provisions
  it, which is what open finding A qualifies.
- [skills.md](skills.md) — the three shipped skills this obligation stands alongside.
- [traceability.md](traceability.md) — where each judging criterion's evidence sits.
- [glossary.md](glossary.md) — `Agent_Skills_Repo`, `Disposition`,
  `Erasure_Certificate`, `Ledger_Checkpoint`.

_Requirements: 27.10, 27.12, 39.8._
