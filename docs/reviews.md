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

Entries I through U come from later passes over the documentation and the gates against
the tree, and the entries from O onward have one thing in common worth stating once: each
survived because a gate reported green over less than it was believed to cover. Each
is recorded with its resolution where one is delivered, and each names the artefact that
carries the change rather than a line of code, because what was read was the module's own
account of itself and the migration that made the change.

| # | Finding | Status |
|---|---|---|
| A | Three discrepancies between `provision_roles.sh` and the requirements | All three closed: two in the script, and the documentation disagreement corrected in the glossary and recorded in migration `018` |
| V | Two console read-only views read five tables the read-only role was never granted | Closed — migration `018` grants them, and it carries the correction migrations `007` and `014` cannot state |
| W | Three stacks handed a provider credential without selecting that provider | Closed — each selects the delivered implementation, and a new case derives the credential-implies-selection claim from the templates |
| B | The tool server's HTTP transport authenticates no caller | Open by acceptance, and reported by a deliberately failing test |
| C | Three of the seven named threats are accepted in part | Open by acceptance |
| D | The design's statement that migration 014 lacks the writer grant on `erasure_lease` | Stale — the grant is delivered |
| E | The console runs as the eraser role while the sensitivity analyser requires the reader role | Closed in the delivered template; a residue remains for an unprovisioned deployment |
| F | Live certificate verification and the public key source | Wired, and a certificate now verifies as one run in `tests/e2e/`; the route's own docstring is stale |
| I | What a read-only demonstration exposes to an anonymous visitor | Closed in the code; untested against a reachable network, because nothing is deployed |
| J | Erasure deletion ordering, and one evidence write the fence does not reach | Ordering closed in migration 017; the fencing gap is open and named |
| K | Whether a certificate's issuer controls the check its verification performs | Closed — the expectation and the owed query set are the verifier's, not the document's |
| L | The recall query text, which is what a person typed | Closed — redacted before it is recorded, with the disabled case recorded |
| M | The replayable window was twice the configured maximum request age | Closed — one-sided bound with a fixed skew allowance |
| N | Whether the scheduled checkpoint rule had a real entry point behind it | Closed in the code; no schedule has fired, because nothing is deployed |
| O | Two test modules shared one basename, so a whole-tree collection failed to import | Closed — no two test modules share a name in the tree now |
| P | The read-only-handle gate silently stopped covering a console module | Closed — the gate names the row-recording routes and holds them to the route table |
| Q | Two test modules encoded the two-sided ingress replay window | Recorded; the source bound is one-sided, and the modules restating the old width are being corrected by their owner |
| R | A deployment pre-condition gate that failed on every checkout | Closed — it passes a clean checkout and fails on an unaccounted placeholder |
| S | Two erasure evidence writes outside the fence | Recorded; the run row's backup naming is fenced, the other two writes are not yet |
| T | The property-deadline gate exempted the case it exists to catch | Recorded, fix in progress — the exemption still stands in the gate |
| U | The seed verb's reset flag removed nothing | The silence is closed: the flag reports its own inertness. Removing rows is not delivered |

### A. Three provisioning discrepancies

The note above `provision_auditor()` in `scripts/provision_roles.sh` records three
places where that function and the requirements did not line up. Two of them are
now closed in the script itself and the third is left as it stands, because closing
it in that file would widen an auditor's reach rather than narrow it.
[auditor.md](auditor.md) documents the surface the script actually creates.

1. **A named as-of-attribution view.** Closed. Requirement 43.11 has the auditor
   gateway expose the as-of query of 43.4 through the read-only view set, and
   `auditor_<SLUG>.attribution_as_of` is created beside the other three and filtered
   the same way, projecting the columns the `binding_as_of` index stores and leaving
   the interval containment predicate as a `WHERE` the auditor writes. The
   caller-supplied instant of 43.4 stays caller-supplied, because a view takes no
   parameter.
2. **A control-plane service account per auditor.** Closed. Requirement 24.4 asks
   for one per auditor, and the function creates it through the same path the four
   service roles use, before the credential check, so a re-run over an auditor whose
   connection string is already stored still establishes the account. The expiry
   obligation is unchanged: the database login keeps its validity bound.
3. **`molt_reader` does not back the auditor views.** Closed. The script grants
   `SELECT` directly to the per-auditor login and never grants the reader role to it,
   so that login cannot read the tables the reader role can. Granting the reader role
   to an untrusted external party to settle a documentation disagreement would widen
   its reach to everything that role can see across every tenant, so the script keeps
   the narrower grant and the documentation is what moved. [glossary.md](glossary.md)
   is corrected — the role entry, the auditor entry, the gateway entry, and a
   near-neighbour row now state the per-auditor grant and the reason for it. The prose
   of `007_roles.sql` and of `014_grants.sql` still carries the old claim and cannot be
   edited: the runner records a digest per applied file and refuses to run when one
   stops matching, so a correction there is a new numbered migration carrying the
   corrected statement in its own prose, never an edit. `018_console_reader_grants.sql`
   is that file — the first later migration to touch the reader's grants, which is what
   makes it the vehicle rather than a file written only to hold a comment.

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

A later pass over the same finding found it was not one view but most of the
console. The reader handle had been wired into the sensitivity route, and every
other read-only view — the fleet overview, the Session detail, lineage, residue, the
run detail and redaction comparison, retention, procedures, the tier view, and the
shared Client roster read all three of the first ones perform — kept reaching for
the handle in scope, which was the eraser one. Two consequences, one cosmetic and
one not:

- The read-only-ness of those views was a habit of each module rather than a
  privilege the cluster held them to.
- The certificate route's live verification could not complete in any deployment.
  `require_reader_role()` refuses a connection whose *configured label* is not the
  read-only role, so the verifier declined the eraser handle before reading
  anything, and the page reported the verification as unattempted with a 503. The
  auditor's primary workflow was unreachable through the console for a reason no
  test covered.

`Console.read_only_store()` is the resolution: the reader connection where the
deployment configures one, and the primary handle where it configures none. It does
not refuse, because a fleet listing makes no claim a wider role would falsify and a
single-connection deployment would otherwise lose the view; the sensitivity route
keeps the strict `reader_store()` for the opposite reason. Every read-only module
now reads through it.

The invariant is enforced rather than remembered, because it already decayed once:
`tests/unit/test_console_read_only_handles.py` derives from the route table which
modules write nothing and refuses any of them that names `store`. A route classified
as a mutation only because it is a form submission — the live verification is the one
such route — is identified by the disposition demonstration mode gives it, so the
classification stays the table's answer rather than a list of exceptions.

### F. Certificate verification and the public key source

Recorded in an earlier pass as unwired. It is wired now.
`src/molt/attest/keys.py` carries `KmsKeys`, `StoredPublicKey`, and
`public_key_source`, and `src/molt/console/routes/certificates.py::_key_source()`
resolves through `public_key_source` against the console's configuration surface,
raising only where that surface names no signing key at all — which is reported as a
missing component rather than as a verification outcome, because reporting an
unprovisioned deployment as *failed* would libel a valid certificate.

The half of this finding that said the path was covered in parts and not as a run is
closed. `tests/e2e/test_full_flow.py` assembles, signs, and verifies a certificate in
one history against a live instance, and the outcome it asserts is the verified one,
so the component coverage — `tests/unit/test_key_service.py`,
`tests/integration/test_certificate_assembly.py`,
`tests/integration/test_checkpoint_verification.py` — now sits under a whole-run
check rather than in place of one. The signature there is produced by a key generated
in the test process, so what the run exercises is the assembly and verification path
and not the key service's participation in it.

The one thing that remained — the module docstring of that route still saying this
build carries no client for the key service — is corrected. It now records what the
route resolves through and why the handle it reads with is the read-only one, which is
the half of this finding that finding G below turned out to depend on.

### G. Three environment variables no process reads

Every deployed component was given its connection parameter and its role under names
the configuration surface declares nothing for: `MOLT_STORE_DSN_PARAM` where the
surface declares `MOLT_DSN_PARAM`, `MOLT_STORE_ROLE` where it declares
`MOLT_DB_ROLE`, and `MOLT_PARAMETER_PREFIX`, which nothing reads at all. The
templates set them, a deployment would show them set on the function, and each
process read none of them.

The consequences were not cosmetic. The connection parameter carries no default,
because a default naming where a credential lives would ship a reference no operator
chose — so every component would have refused at startup for a value an operator
could see was set. The role label falls back to `writer`, which is the label
`require_reader_role()` refuses, so the MCP server and the auditor path would have
declined their own read-only reads while connected through the reader connection
string. A label is a claim and a connection is a privilege, and here the claim was
wrong in the direction that fails safe but fails.

The templates now use the declared names. What let this stand was that nothing
asserted the names at all: `tests/infra/test_templates.py` read the templates
closely for charged resources, embedded secrets, and policy scope, and never asked
whether a variable it saw was one a process reads. Three cases were added — every
`MOLT_`-prefixed variable a template sets is a key the surface declares, every stack
that connects to the cluster sets both the parameter and the role, and the role each
stack declares is the one its connection parameter path names, so a stack pointed at
the reader connection cannot label itself something wider.

### H. The bounded serving loop counted its own silence

The hosted tool transport polls its socket with a short timeout so that a stop is
noticed without a signal, and served a bounded number of requests by counting one per
iteration of that poll. An expired poll is not a request, so the count was of
iterations and the bound was a duration in disguise: at a 200 ms poll and a bound of
ten thousand, `molt mcp --transport http` ended after a little over half an hour of
quiet and reported ten thousand requests answered, which was the count it was asked
for rather than the count it reached.

The same defect showed up first as an intermittent test: a handshake over a real
socket failed under parallel load, because the client took longer than one poll
interval to connect and met a loop that had already finished its single request.

The loop now counts answers, reported by the handler after the body is written, and
the verb reports what was answered rather than what it asked for. Coverage:
`tests/mcp/test_transports.py`, including a case that waits several poll intervals
before asking anything, which fails against the old count and passes against the new
one.

### I. What a read-only demonstration exposes

A public demonstration serves a visitor holding no operator credential, which puts two
things at risk at once: a route that changes stored memory being reachable anonymously,
and a demonstration roster reaching a Client a real engagement created.

Both are answered by refusing ahead of dispatch rather than by asking inside a handler. A
per-handler check is opt-in and therefore forgettable, and a handler that forgot would be
exactly the mutation route the mode exists to refuse. The gate is middleware added last so
it is outermost, it runs before authentication is consulted and before a handler is
chosen, and it decides from the route's *declared disposition* in the route table. Four
consequences are worth naming because each is a failure mode closed rather than a feature
added:

- A blocked route is refused even where no view module has claimed it, so a view written
  later inherits the refusal rather than acquiring one.
- The refusal is `403` and not `404`. The route exists and is refused on policy, which is
  what the demonstration is meant to show: the erasure console is present with its
  controls disabled rather than hidden.
- A route name the table does not classify is refused rather than allowed, so forgetting
  to classify produces a visibly refused route instead of a silently open one.
- A hidden read route stays reachable, because refusing it would remove the thing the
  demonstration is for.

The tenancy half is narrowed at the one place tenancy is produced: the shared Client
roster read, on the console's own mode flag rather than on anything a caller passes, with
the readable set taken from the seed corpus itself rather than restated. A view added
later has no unnarrowed roster available to ask for.

Outstanding, and it is not small: nothing is deployed, so this posture has never faced a
reachable network. The tool server's transport is a separate posture and remains open by
acceptance — see finding B.

### J. Erasure deletion ordering, and the one write the fence does not reach

Two gaps found in the same pass over the erasure path, one closed and one open.

**Ordering.** Three references declared in the first migration generation stood between an
authorised erasure and the rows it was authorised to remove, in a shape no delete order
can satisfy: a sub-agent Session names the Event that spawned it while every Event names
its Session, which is a cycle, and the parent-Session and answering-Event references are
the same problem inside one table, where a batch boundary can fall between the two halves
of a pair that leaves together. Migration `017_erasure_references.sql` drops the
enforcement and keeps the record — every column stays, every index over it stays, and the
derivation graph is carried independently in `lineage_edge`. `ledger.session_id` stays
enforced, because it forbids an orphan Event and sits in no cycle, and the disposition
phase orders its hard deletes so that every Event is removed in an earlier batch than, or
the same batch as, any Session. The same answer this schema had already reached twice for
checkpoint session identifiers and disposition artifact identifiers: a reference that
would either refuse the erasure or vanish with it defeats the record it was meant to
protect.

**Fencing.** Every evidence write the erasure engine sends presents the generation the
worker believes it holds — the working-tier purge, phase one's candidate set, the residue
phase's extensions of it, each disposition, the phase marker, and the completion of the
run and its request. The one write on the path the fence does not yet reach is the residue
detector's own per-finding recording, which frames its own transactions inside the module
that owns them, so until fencing lands there a superseded owner can still record a residue
candidate row. That is stated in the engine's own module documentation rather than only
here. It stays open, and it is narrow: a superseded owner cannot delete, cannot dispose,
cannot move the phase marker, and cannot declare anything finished.

Related: when a write is refused as stale the run ends aborted and the evidence written
under the valid generation is kept, because removing true evidence to tidy up a false
claim would be the worse outcome.

### K. A certificate's issuer must not control the check

A certificate carries embedded SQL a hostile reviewer runs against the live cluster. The
finding is what would happen if the document also carried the *expectation* each query is
judged against, or the list of queries the certificate owes: an issuer could then weaken
the one check a reviewer relies on by declaring a laxer expectation beside the query, or
leave an inconvenient template out and have the omission read as nothing at all.

Neither is a field of the document. Both are properties of the verifier's fixed template
set: the expectation belongs to the template, presence of every obligatory template is
required rather than assumed, and a departure from the fixed set is reported as a failed
check rather than as a note — a note would leave the outcome `verified`, and the
divergence is exactly the lever an issuer would reach for. The signature is checked in
process against a retrieved public key, and the key service is never asked whether a
signature is good, so a reviewer who never held permission to call that service verifies
exactly as well as the issuer does.

### L. The one field of the recall path that is prose a person typed

Recall writes an Event recording what was asked and what came back. Four of its payload
fields are identifiers, distances, and counts. The fifth is the query text, which is
whatever someone put in a prompt — the one place in this system where a secret arrives as
ordinary prose. The Ledger is append-only and no role holds `UPDATE`, so text written
there leaves only by an authorised erasure or by expiry: the write is the last moment
redaction can happen at all.

So the text goes through the Redactor on its way into the payload, and the Event's
redacted flag is set from what the Redactor reported rather than asserted by the caller.
Where redaction is switched off by configuration the text is recorded as it arrived, and
the recall path itself writes the warning record naming the Session whose query text was
recorded unmodified, because the Redactor reaches no telemetry surface of its own and
would otherwise leave that record to nobody.

The residue is the configuration: an operator can disable redaction, and what the system
guarantees in that case is that the choice is recorded against the Session it affected,
not that the text was protected anyway.

### M. The replayable window was twice what it was configured to be

The signed ingress bounds replay by refusing a request whose timestamp is too far from the
Collector's own reading. The finding is that the check treated a timestamp ahead of the
reading exactly as it treated one behind it, so a request stamped into the future was
admitted as far forward as a stale one was admitted backwards, and the window an operator
configured as a maximum request age was twice that wide in practice.

The bound is one-sided now: staleness is admitted up to the configured maximum, and a
timestamp ahead of the reading is admitted only as far as a fixed clock-skew allowance. A
timestamp naming no instant at all is refused as outside the window rather than parsed
into a plausible one, and a refusal records the cause, the configured maximum, the
allowance, and the age presented, so an operator can tell a clock problem from an attack.

This narrows replay rather than closing it, which is the second item of finding C. A
per-request nonce table would close it and would put a write-contended row in front of
every capture.

### N. The scheduled rule and what stands behind its name

The console stack declares a scheduled rule that fires at a configured interval and
carries an entry-point name in the event body rather than targeting a second function —
deliberately, because the signing permission is granted to that one execution role and a
second signing principal would end the exclusivity the checkpoint rests on. The finding
raised here was whether anything behind that name actually took a checkpoint. A rule
firing into a placeholder is the worst shape this can take: the schedule exists, the
invocations succeed, and every document says checkpoints are being taken while nothing is
stored.

The delivered function dispatches on the entry-point key and refuses a name it does not
host. The checkpoint entry point resolves the signer and the policy from the configuration
surface, takes the checkpoint through the store the container already holds rather than
opening a second connection that might authenticate as a different role, and derives the
window's closing instant from the instant the rule fired rather than from the clock it
reached, so consecutive windows meet at the schedule boundary instead of at boundaries
jittered by invocation latency. Two outcomes are distinguished on purpose: no provisioned
signing key is reported as having signed nothing, while a signing failure, a refused
transaction, or exhausted retries are raised, so the invocation lands in the scheduler's
own failure count and its retry covers the interval it was owed.

Outstanding: no schedule has ever fired, because nothing is deployed. What is verified is
the entry point, not the schedule.

### O. One basename, two modules, and a collection nobody ran

Two test modules carried the same file name in two suite directories, and neither
directory holds a package marker, so a collection over the whole tree resolved both to one
module name and failed on the import mismatch before running anything.

Why it survived is the whole finding. The canonical runner invokes the suite directories
as separate processes, and no single process ever imported both modules, so every green
run was green over a collection that never met the collision. A reviewer running one
`pytest` over `tests/` met an error that the project's own command could not reproduce.

Resolution: the module-scoped console gate is named for what it scopes,
`tests/quality/test_console_module_store_scope.py`, beside the handler-scoped gate that
keeps the original name under `tests/unit/`. No two test modules in the tree share a
basename now, so a whole-tree collection and the canonical runner collect the same set.

### P. The gate that inferred one fact from a different one

The gate holding the console's read-only modules to the narrow database handle decided
which modules to check by asking which of their routes record rows, and it answered that
question from the disposition a demonstration gives each route: a mutation the table
blocked recorded rows, and a mutation it admitted was a form submission that merely
carries the session's request token.

The route table then grew an import-time invariant obliging every route declaring itself a
mutation to declare the blocked disposition, because a demonstration exposes no mutation
route at all. The inference went degenerate the moment that landed. The live certificate
verification recomputes a digest and stores nothing, and it now declared the same
disposition as the route that records an erasure run, so the certificates module fell out
of the gate's scope in silence. The gate kept passing over a smaller set — which is the
same failure this gate was written to catch after the invariant it protects had already
lapsed once.

Resolution: the routes that record rows are an explicit set naming the row each one
writes, held to the route table so a set naming a listing, or a route the table does not
declare, fails rather than exempts, and the checked set carries a named floor so a
derivation returning nothing fails too. The certificates module is inside that floor
again.

### Q. Two modules still stated the window the source had narrowed

The signed ingress bound is one-sided in the source: staleness is admitted up to the
configured maximum request age, and a timestamp ahead of the Collector's reading only as
far as a fixed clock-skew allowance, which is the resolution recorded as finding M. Two
test modules still encoded the earlier symmetric window, so what they asserted was the
width the design had deliberately stopped having.

Why it survived: an assertion restating an old bound passes for as long as the new bound
is narrower than the old one in the direction the assertion happens to test. Nothing
disagreed, so nothing reported.

Status: recorded, and the source side is verified — the check refuses a timestamp further
ahead of the reading than the allowance, and a refusal records the cause, the configured
maximum, the allowance, and the age presented. The two modules restating the old width are
with their owner; this entry does not claim that correction has landed.

### R. A pre-condition gate a reader learns to ignore

The infrastructure gate over the deployment parameter file asserted that no placeholder
remained anywhere in it. Several must remain in a checkout: an archive location a build
prints, an image reference nothing in the tree publishes, and the account's own model
resource names. So the gate failed on every clean checkout by construction.

That is worse than an absent gate. A check that always fails teaches a reader to skip its
line, and the one signal it could have carried — a parameter added with a placeholder and
forgotten — arrives in exactly the same shape as the failure they have learned to skip.

Resolution: what is asserted is now that every placeholder still held is one the
declaration accounts for, with the mechanism that supplies it, and the reverse direction
is asserted beside it so an entry cannot outlive the gap it names. A third case holds each
entry to the tree: the parameter is one a template takes, the mechanism named is a file
that exists, and the kind of supply agrees with what the tree does. The gate passes a
clean checkout and fails on drift.

### S. Two evidence writes the fence does not reach

Every evidence write on the erasure path presents the generation the worker believes it
holds, which is what makes a superseded owner's write refusable. Two writes stood outside
that: the residue detector's per-finding recording, which frames its own transactions
inside the module that owns them, and the insert of the backup record.

Why it survived: the fence is applied per write rather than by a wrapper around the phase,
so a write added inside a sub-component inherits nothing, and no test asked which writes
present a generation — only that a refused one persists nothing.

Status: recorded, and partly closed. The run row's naming of its own backup evidence goes
through the fence under its own label, verified in the engine. The backup record insert and
the residue detector's per-finding write show no generation, so a superseded owner can
still record those rows. The narrow shape of that residue is unchanged from finding J: a
superseded owner cannot delete, cannot dispose, cannot move the phase marker, and cannot
declare anything finished. The remaining fix is with the owner of those modules.

### T. The deadline gate exempted the case it exists to catch

Every property module is obliged to disable the per-example wall-clock deadline, because
wall-clock time in a generative suite is a function of how much else is running and a
deadline turns machine load into a reported correctness failure. The gate over that
convention reads each module's settings decorators and refuses one that configures a
property and leaves the deadline in place.

A module carrying no settings decorator at all is treated as not a finding, on the reading
that it takes the library's defaults deliberately. Those defaults carry a wall-clock
deadline, so the exempted case is precisely the case the convention exists to prevent: a
module that passes on an idle machine and fails on a busy one, with no line in it that a
reader could point at.

Status: recorded, and the exemption still stands in the gate as delivered. The fix is with
the owner of that module.

### U. A flag that was accepted and did nothing

The seed verb accepts a reset flag. Nothing behind it removed a row, so an operator
seeding over an existing corpus got a second corpus and a flag that reported success.

Why it survived: the flag parsed, the verb exited zero, and the outcome an operator
expected — a clean corpus — is indistinguishable from a fresh instance in every check the
suite makes.

Status: the silence is closed. The verb warns that the flag removes no row in this build
and says to seed into an empty corpus instead, so the flag no longer claims an effect it
has not got. Removing rows is not delivered, and this stays an outstanding capability
rather than a fixed defect.

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

### V. Five tables two read-only views read and could not

The reader's grant list was assembled twice, in the roles migration for the tables the
first generation created and in the grants migration for the tables the second created.
Both times the list was drawn from the read-only *components* that existed when the file
was written, and a table that only a console view reads was in neither.

Why it survived is the whole finding, and it is the same shape as the earlier reader-grant
gap: the console read through whichever handle was in scope, and the handle in scope was
the eraser's, which can read everything. The views were exercised under a role wide enough
to hide the omission. Narrowing them onto the read-only connection — which is what made
their read-only posture a privilege rather than a habit — is what turned a latent gap into
a failure, in two views a reviewer opens rather than paths an operator can route around:
the memory-tier view counts the erasure request and the backup record among the action
tier's tables and counts, expires, and reads back the configuration of the working tier's,
and the approval queue's listing reads the queue joined to the rule that raised each entry.

Status: closed. `018_console_reader_grants.sql` grants `SELECT` on those five tables and
nothing further. The narrowness matters twice over rather than as a formality: the working
tier is the one tier whose rows are freely overwritten, so a wider grant would let a
read-only page disturb the numbers it exists to report, and an approval is a record of a
human decision that the role rendering the queue must not be able to answer.

Unverified: no deployment configures a read-only connection yet, because nothing is
deployed. What is established is the grant and the schema shape, not a served page.

### W. A provider credential with no provider selected

Three deployed stacks were each handed a model-provider credential and never told which
provider to resolve. Both selection keys carry a default, so nothing refused for the
missing selection: each process resolved the documented default implementation while
holding the other implementation's credential. The default's own builder then demands a
region that no template sets, so the deployment would have failed at startup naming a
missing region — a fault that names neither the credential nor the selection nobody made,
which is the worst shape a configuration error can take.

Why it survived: the gate over the templates asked whether every configuration variable a
template *sets* is a key the surface declares. A variable a template *omits* is invisible
to that question, and the omission here was of exactly that kind.

Status: closed. Each of the three stacks now states the delivered selection with the
reason it does not take the default, and supplies the model identifier that selection then
requires — as an operator input in the parameter file's placeholder form, because the
repository grounds no verified model identifier and inventing one would be worse than
owing it. A new case derives the claim from the templates: every stack given a credential
for a provider role selects that role's provider, and the selected name is one the
registry holds. That case is what found the third stack, after the first two were fixed.

### X. A placeholder where the platform admits only a resource name

The two roles carrying the documented default provider's `bedrock:InvokeModel` grant
named their models through a parameter the parameter file filled with a placeholder. The
platform refuses a permission whose resource is neither a well-formed name nor a
wildcard, so this was not a value awaiting an operator. It was a stack that could not be
created, and it failed exactly that way: `molt-roles` rolled back twice, once per role,
each time naming the placeholder it had been handed.

Why it survived is a gap between two true statements. The outstanding-value accounting
said these were account resource names only the target account can state, which is
correct, and it asserted that each was named as the resource of the one operation it is
owed for, which is also correct. Neither says anything about whether a deployment can be
created while the value is owed. Every other outstanding value is one a stack takes
without a policy reading it — a bucket, a key, an image reference — so the whole set had
been reasoned about as *values a first deployment does without*, and these two were not
that.

Status: closed. Both names now default to empty and both grants are stated only when a
name is supplied, so the delivered deployment holds no model permission rather than a
broken one. The two alternatives were each rejected for a stated reason: a wildcard would
buy every model in the account to keep a statement the delivered configuration never
reaches, since the delivered selection is the external provider for both provider roles,
and deleting the statement would remove the documented default path with nothing naming
what to grant. Supplying either name restores the grant with no template change.

The accounting changed shape with it. Neither value is owed any more, so both entries
were removed and the outstanding kind that held only them went with them. What replaced
them is a case that reads the delivered configuration: each name admits emptiness, is
delivered empty, every grant of the operation anywhere is conditional, each condition
tests its own parameter against emptiness, and no such grant names an open resource. The
last clause is the one that matters — it refuses the wildcard shortcut rather than
trusting nobody takes it.

One assertion in the suite was widened to keep this honest. A statement written under a
condition is now resolved wherever statements are walked, so wrapping a grant in a
condition changes when it is created and not whether the privilege checks see it.
Otherwise a condition would have become the place to hide a permission from the gates
that count them.

Verified: `molt-roles`, `molt-kms`, and `molt-storage` create.

### Y. The same interpreter defect in five more scripts

The deployment script named the platform interpreter unconditionally, so the parameter
validator crashed on every stack for want of the pinned document parser. That was found
and fixed. What was not asked is how many other scripts held the same line, and the
answer was five: both provisioning scripts, the packaging script, the local-instance
script, and the teardown script.

Two of the five are worse than an inconvenience. `provision_cluster.sh` runs the
capability probe, which imports the package itself, so on a machine where the package
lives in an environment the script does not resolve, the probe dies on an absent module
*after* the cluster is created and the migrations are applied — leaving a provisioned
cluster with no capability record, which is the one artefact every component reads at
start-up and branches on. `provision_roles.sh` composes each role's connection string
through a helper of the repository, so the same failure lands between creating the roles
and writing the parameters they are reached through.

Why it survived: the fix to the deployment script was made as a repair to that script.
Nothing compared one script's resolution to another's, so five identical lines sat in the
tree while the sixth was being reasoned about.

Status: closed. All five resolve in the order the test runner uses — an override, then
the shared environment, then one inside the working tree, then the platform interpreter.
The gate is new and is the point of the entry: rather than asserting any one script is
right, it asserts that every script naming an interpreter names all four candidates in
that order, that none pins one, that each reads the override with a default expansion so
the unset-variable option does not abort the branch meant to be optional, and that the
platform interpreter appears once and last so no branch after it is unreachable. A sixth
copy of the defect fails it, and so does a script that invents a different order.

### Z. Full certificate verification with no authority to verify against

Every service role's connection string was composed with full certificate verification
required and no authority set named. That is not a stricter check than naming one — it is
no connection at all. The client looks for an authority file under the calling user's
home directory, and no function runtime and no task image holds one, so each deployed
process would have refused to connect before a request left it, reporting a missing file
in a home directory: a fault that names neither the cluster, nor the credential, nor the
deployment.

Why it survived: the requirement was asserted and the authority was not. Several cases
checked that the mode was `verify-full` and that a weaker mode was refused, which is the
half a reviewer thinks about. Nothing asserted the half that makes the mode usable, and
the composing step ran on a machine that happened to have an authority file where the
client looks.

Status: closed, and the fix is not where the defect was found. The composer now names the
platform's own authority set, but a path cannot be settled there at all: the machine that
writes a connection string into the parameter store is not the machine that dials it. So
the resolution moved into the store, the one place every connection passes through, which
resolves the portable keyword to a bundle the connecting process can actually see and
leaves an authority an operator named explicitly alone. That distinction was not
theoretical — the keyword resolved on the deployed runtime and failed on the machine that
composed the string, because what it resolves to depends on how the driver's bundled
cryptography library was built rather than on the platform running it.

Verified: the deployed console reports the cluster reachable, and all four service roles
authenticate from this machine against the managed cluster.

### AA. Three defects in the role provisioning, each on the one run that matters

The script that creates the four database roles and stores their connection strings had
never been run end to end. Three faults sat in it, and each surfaced only on a first real
provisioning run.

The control-plane invocation passed the service account's name as a named option where the
interface takes it positionally, so the whole invocation was refused on an unknown flag.
The cluster creation in the sibling script had the same shape and would have failed the
same way, on the one run that had a cluster to create.

The grant applied its login suffix twice — the format string carried it and so did the
argument — so the statement named a login nothing had created and the file failed partway
through, after the login existed.

The third is the one worth reading. Creation and the password were a single statement
under *if not exists*, which is a no-op when the login is already there. Combined with the
failure above, a second run would have left the login holding the first run's password
while storing the second run's in the parameter store: a credential that cannot
authenticate, stored as though it could, with nothing failing until a deployed process
tried it and reported a refused login. The password is now set by its own statement, on
the reasoning that reaching that point at all means the parameter held no value, so any
existing login's password is unknown and setting it is the only way to make the stored
string true. The auditor path carried the same shape and was corrected with it, including
its validity bound.

Status: closed. All four roles provisioned, and each stored connection string was verified
by connecting with it and reading back its inherited privileges: the writer can insert
into the ledger and cannot delete from it, the eraser can delete and update an erasure
request and cannot insert a session, and the reader and the watcher can do neither.

### AB. A compatibility tag older than the wheels the pinned driver publishes

The function package resolved its dependencies against a single platform tag, and the
pinned database driver publishes no wheel that satisfies it. Resolution failed naming the
pinned version as though it were not on the index, which is the least helpful way to
report a tag that is too old.

A tag is a floor on the platform's libraries rather than a name for it, so more than one
may be named and each dependency resolves to the newest wheel it actually ships. The
deployed runtime satisfies both.

Status: closed. No gate: the packaging step fails loudly and uploads nothing, so this
cannot reach a deployment unnoticed — which is the opposite of every other finding here
and is why it gets a note rather than an assertion.

### AC. A setting read by its key where its name was required

The configuration surface gives every setting two spellings: an environment variable name
and a dotted key. The accessors resolve by the name alone, so passing the key is not a
second way of asking — it is a read that cannot succeed.

The capability probe named two settings by their keys, so every attempt to build a model
provider raised an unknown-setting fault. The failure was quiet: the probe reports a
provider it cannot build as a warning and still exits successfully, so the prompt-cache
capability was left unprobed on every cluster the probe had ever been run against, and the
text provider always took the unprobed path. Nothing failed; the record simply had a hole
in it, and the hole looked like a fact about the cluster.

Status: closed. A new gate walks every configuration read in the package and the scripts
and asserts that a literal setting name is one the surface declares, reporting the exact
substitution to make. It reads the call sites rather than searching for keys, because both
spellings are legitimate strings to hold and only the position makes one wrong. The
secret-only settings are admitted alongside the general surface, since they have a name
and deliberately no key.

Verified: both providers now build against the live cluster, and the probe reports the
distributed vector index available with its operator class, rangefeeds enabled, and both
model identifiers reachable.

### AD. A concurrency ceiling the account could not grant

The ingest function declared a reserved concurrency as a plain number. The platform
refuses a reservation that would leave the account's unreserved pool below a floor of its
own, so an account whose entire allowance equals that floor — which is the allowance a new
account starts with — can grant no reservation at all, of any size. The function was
impossible to create there, and the reported fault was about the account's unreserved pool
rather than about a ceiling anyone had chosen.

Neither obvious escape is right. A reservation of zero throttles the function to nothing
rather than leaving it unreserved. Deleting the property drops the ceiling for every
deployment, not only the one that cannot have it.

Status: closed the way the model grant was: the value admits a word meaning *state no
ceiling of this kind*, the property is stated only otherwise, and the default remains the
documented ceiling so a deployment that names no value still gets the posture the design
asks for. What the word gives up is stated rather than hidden — a reservation is both a
ceiling on ingest and a guarantee for every other function, so omitting it leaves ingest
sharing one pool with the console, bounded by the account's allowance instead of by this
value. The gate asserts both halves, because a conditional property whose default was the
omission would mean most deployments ran unreserved with nothing saying so.

### AE. A task execution role that could not pull the image it was given

Both task stacks granted the four registry operations and the two log operations in one
statement whose single resource was the log group. A statement's resource is what its
actions are granted over, so the registry grant was scoped to a log group and no task
could ever pull its image. Nothing refused the template: every action was legitimate, the
resource was a real name, and the statement read plausibly. The service reported it as
being unable to place a task, naming an authorisation call and saying nothing about a
resource being wrong.

Status: closed. Three statements: the token call, which accepts no resource of its own; the
image reads, scoped to one repository named as a parameter rather than parsed out of the
image reference, since a reference may name a digest, a tag, or another account's registry;
and the log writes.

Two gates rather than one. The open-resource rule now admits an operation that accepts no
resource only when it is alone in its statement, which is what stops an open resource being
reached by bundling a second operation alongside it. And a new case refuses any statement
granting operations of two services under one resource, asserted on the service prefix
rather than on this pairing, because one resource cannot be correct for two services and
the general form is what catches the next instance.

Verified: the watcher task pulls and runs, probes the change stream, records the answer, and
degrades to the timestamp poll — the documented fallback, observed rather than asserted.

The conclusion drawn from that observation was wrong, and finding AS below is the correction.
The degradation was read here as the cluster serving no changefeed. It serves one. The watcher
was never given anything to open a stream with, refused its own stream before the cluster was
asked, and reported that refusal in the words of a cluster refusal.

### AF. A tool server delivered as a service it cannot be

The tool server serves two transports: one reads a peer on its own standard input, the
other listens. The task template stated neither, so the process resolved the surface
default, got the process transport, served its tools to nobody, and exited. The service
restarted it about once a minute, and every restart logged a clean start-up and a clean
shutdown, which is the least alarming way for a deployment to be broken.

This is the same shape as the provider-selection finding: a variable a template omits is
invisible to a gate that checks the variables a template sets.

Status: closed. The transport is stated, and the delivered desired count agrees with it —
zero, because a service of a process-transport server is a restart loop rather than a
running server. What this stack deploys is the task definition and its two roles, which is
what an agent's own runner uses; the running copy of the tool server is the one the agent
starts beside itself. Raising the count is only meaningful alongside the network transport,
and that transport authenticates no caller, so the task security group admits nothing
inbound and nothing could reach it here regardless. The gate asserts both the explicit
transport and the agreement, since the variable alone would have passed the configuration
that was actually deployed.

### AG. Eighteen written console handlers that nothing attached

Every page of the console answered *not implemented*. The route table was complete, the
eighteen view modules were written and correct, and the view package's own documentation
said that importing it is what attaches the handlers. Nothing imported it.

So every route resolved to the placeholder — the answer meant for a route declared ahead
of its handler, which is a reasonable thing to return for one route and an unreasonable
thing to be true of all of them. Nothing failed. The status is well-formed and the body
names the route.

Why it survived has two parts, and both matter. The suites that exercise the handlers
import the view modules themselves, directly, so they passed against handlers the
application never attached — the tests and the application disagreed about what was
running, and only the tests were ever consulted. And the import cannot be written at
module level: every view module imports the request helpers from the application module,
so importing the package from there closes a cycle and fails at start-up. That is
presumably how it came to be missing, and it means the fix had to be placed rather than
merely added.

Status: closed. The view package is imported inside the call that builds the routes, where
there is no cycle because the application module is fully initialised before anything calls
it. The gate asserts the join rather than the halves: every declared route resolves to a
claimed handler, and no built route is answered by the placeholder — read off the routes
the application actually builds, through the same call, so an import the application does
not perform is one the gate does not see either. A second case checks the built endpoints
rather than the handler table, because a lookup by the wrong key would populate the table
and still select the placeholder.

### AH. What this account will not host, which is not a defect

Two things stayed undeployed for reasons outside the repository, and both are the same
reason: the account is unverified, and the provider restricts public-facing resources until
support verifies one.

The content distribution is refused outright at creation, naming account verification. And
the two function endpoints, though configured for anonymous access and carrying a resource
policy that allows it, answer every anonymous request with a refusal from the platform
rather than from the application — the policy is correct and the account is not permitted
to use it. An earlier session had recorded this as an unexplained refusal and guessed at an
organisation policy; the account is in no organisation, so that guess was wrong and the
verification state is the whole of it.

Neither is worked around, because both workarounds would be worse. The distribution is
skipped through a new flag rather than replaced, and the deployment script now announces
each omission, because a stack that was never created is a missing part of the deployment
and a later stack resolving a value from it should say so rather than deploy something
half-wired. The function endpoints are exercised through direct invocation, which is the
same code path a request takes and proves the same things about it.

What is genuinely outstanding is therefore one support request, and everything behind it is
built, deployed, and verified.

### AI. The restriction was real and the conclusion drawn from it was wrong

The previous entry recorded two refusals — a content distribution refused at creation, and
every anonymous request to a function's own endpoint refused by the platform — attributed
both to an unverified account, and concluded that a public deployment waited on a support
request. The attribution was right. The conclusion did not follow.

What was never checked is whether anything *else* could serve a public request. A regional
endpoint can, and is subject to neither refusal. One was created, pointed at the console
function, and answered: the sign-in page rendered, the health report answered, and an
unauthenticated request to a protected page came back refused by the console itself rather
than by the platform — which is the distinction that had been missing all along. The
restriction applies to a function's own endpoint and to a distribution, and not to a
regional endpoint in front of the same function.

Why it survived is worth naming, because it is not a coding error. The two refusals shared
a cause, and sharing a cause made them read as one wall. Both were about reaching a
function from the public internet, both named the account, and the remedy for one is the
remedy for the other — so the search stopped at the cause instead of continuing to the
question the cause does not answer, which is what else would work. A correct diagnosis
became a reason not to look further.

Status: closed. The `gateway` stack puts a regional endpoint in front of each function.
The console and the ingest function are both publicly reachable, with no distribution and
no support request.

Three things make it the answer rather than a workaround. It costs nothing per hour, which
is the test the cost ceiling applies: a load balancer is ruled out here because it is
charged whether or not anything calls it, and this is charged per request and by nothing
else. It changes no application code and no other stack — one route per endpoint matching
every method and every path, so the request the function receives is the request it
received before, and the route table that decides which paths exist and which authenticate
stays the application's alone. And it is what a distribution would sit in front of: when
verification completes, the distribution takes this endpoint as its origin rather than
replacing it, which is a change of one resolved value.

Two details were load-bearing and neither was obvious. The stage must be the platform's
unnamed one, because a named stage becomes the first path segment of every address while
every link and stylesheet reference in the console's pages is written from the root — a
prefix would serve pages whose every link is broken, which is worse than not serving them.
And the distribution's origin parameter was named for the function endpoint it used to
hold; it now holds a regional endpoint's host, so it is named for what it is. An origin
pointing at the function's own endpoint would have made the distribution reach nothing, and
the failure would have surfaced as the distribution's own error page rather than as
anything about the origin.

The gate asserts what keeps the endpoints transparent rather than that they exist: every
route carries every method and path, every stage is the unnamed one, every permission is
conditioned on its own endpoint rather than admitting any endpoint of the account, and
every stage bounds the request rate. The last one matters more here than it usually would.
The account's whole concurrency allowance equals the floor the platform keeps unreserved,
so no function can reserve any of it, and this throttle is the only bound left on a leaked
credential.

Verified live: sign-in over the public address, all seven console pages rendered from the
managed cluster, the stylesheet and the stream script served with their own content types,
and the ingest function's health report answered.

### AJ. A denial that caught the one principal it was written to except

The signing key's policy grants `kms:Sign` to the console execution role and denies it to
every other principal, so that the administrative statement beside it cannot be widened
into a second signing principal. The exception was written as a not-principal element
naming the role.

That element is matched against the principal that made the request, and a function does
not make requests as its role — it makes them as a session assumed from that role, which
is a different name. So the exception matched nothing the function ever sent, the denial
applied to it, and signing failed for the single principal the policy grants it to.

It was not caught by inspection because the policy reads correctly. Both halves say the
same role, the gate compared the granted principal against the excepted one and found them
equal, and every statement of the claim — in the template, in the gate, in the design — was
true. The claim was about a name that never appears in a request.

What found it was the deployment running unattended. The scheduled checkpoint fired on its
own hour and every run failed with an explicit denial, which is the shape of a *missing*
grant and the opposite of one; nothing else would have looked at those logs. This is the
first defect here that only a deployment left running could have produced, as against one a
first deployment produces.

Status: closed. The exception is a condition on the requesting principal, whose key
resolves to the role for a session assumed from it, so it matches whether the caller is the
role or a session of it. The claim is unchanged and now holds for the caller that exists.

The gate refuses the shape rather than checking the outcome: a signing denial may not be
written with a not-principal element at all, must deny every principal, and must except
exactly the principal the grant names — compared as the role names each side constructs,
since a principal node and a condition value hold the same name under different wrappers.

Verified: the checkpoint signer, invoked against the live deployment, answers `signed` with
a checkpoint identifier and a root digest, and the cluster holds the row.

### AK. A bucket policy that denied its own repair

The evidence bucket denied every operation to every principal outside three roles and the
account root, written the same way as the finding above and wrong for the same reason. The
consequence here was worse than a refused write.

An explicit denial in a resource policy cannot be overridden by any identity policy. The
principal deploying the stack is neither one of the three roles nor the account root, so the
denial covered it — including the operation that replaces the policy. The bucket could not be
repaired, read, emptied, or deleted by the credential that created it, and the stack update
that tried to correct the statement failed on the statement it was correcting. Nothing but
the account's own root credential could reach it.

Two things had to change, and only one of them was the not-principal element. Superseding the
bucket needed the name to be variable without renaming the deployment, and needed the
replacement not to attempt deleting the unreachable one — so the bucket carries retain
policies on both deletion and replacement, which it should have had regardless: a certificate
is tamper evidence held under Object Lock so that nobody can remove it, and a stack deletion
sweeping the bucket away would be this deployment destroying the evidence it exists to
preserve.

The second correction is the one that matters, and the first attempt at it got the boundary
wrong. Rewriting the exception as a principal condition made the statement work — and a
working statement that denies every operation is still a policy that denies its own
replacement, so the superseding bucket was born locked exactly like its predecessor. That
took a second supersede to establish, which is the clearest evidence that the shape rather
than the syntax was the defect.

Status: closed. The denial covers the ways to reach the evidence — reading an object,
writing one, deleting one, enumerating the bucket — and deliberately stops short of the
bucket's own administration.

Nothing is given up by that boundary, and the reasoning is worth keeping because it looks
like a weakening and is not. A principal permitted to rewrite this policy could rewrite it
to permit itself, so the denial never stood between such a principal and an object. What
actually protects a written certificate is Object Lock in compliance mode, which refuses an
overwrite and a deletion for the retention period to every principal including the account
root — and that is declared on the bucket, where no policy edit can reach it. The denial
protects against a principal that can read the bucket and cannot rewrite policy, which is
the threat it was written for; extending it to administration protected nothing and cost the
bucket its recoverability.

The gate asserts both directions: every evidence operation is denied, and no
policy-management operation is, with `s3:*` refused outright since it silently covers both.

Two buckets are left orphaned by the two supersedes. Both are empty — no certificate had been
written, because signing was failing for the finding above — and neither is reachable by the
deployment's own credential. Removing them needs the account root and is housekeeping rather
than a blocker; the deployment reads and writes the third.

### AL. A batch job started from a request, and three hosts that could not finish it

Starting an Erasure_Run from the deployed console did nothing at all, and it did nothing
quietly: the attempt was recorded, the identifier was answered, the progress stream reported
no phase, and no run row existed. Three separate causes sat behind that one symptom, and
each was only visible once the one in front of it was gone.

**A thread the host suspends.** The deployed launcher started a daemon thread and returned,
on the reasoning that the run must not depend on the request staying alive. A function host
freezes the execution environment the moment a response is written, so the thread was
suspended mid-run and resumed only if that environment happened to serve another request.
The docstring's reasoning was right and its conclusion was wrong for the host it named.

Replaced by an asynchronous second invocation of the same function. The same function
deliberately: signing a certificate and writing it to the evidence bucket are granted to the
console's role alone, and a worker anywhere else would need that grant duplicated, which
would give this deployment two signing principals where it asserts one. Which launcher is
used is decided by reading the host from the environment rather than from a configuration
value, so a deployment cannot be configured into the launcher that silently does nothing
there.

**A configuration value with no default.** With the run finally executing, it aborted
immediately on a missing backup target. The console had never set one and the surface
declares no default, so every run the console could be asked to start would have failed —
and the failure surfaced in the second invocation as an aborted run rather than as a refused
request. A console that cannot back up cannot erase, and it should say so when it is
deployed rather than when it is used. Both backup values are now template parameters.

**A cluster that serves no backup.** Then the backup phase itself refused: the plan does not
offer a user-initiated backup, which the capability probe had already recorded rather than
assumed. Runs on this deployment skip the backup, which is a request field rather than a
workaround, and an unticked run refuses at that phase instead of erasing without the
evidence.

**And then the honest limit.** With all three fixed the run proceeds properly — it takes the
lease, detects residue by vector similarity, and works through dispositions — and it does not
finish. Every blended Artifact that must survive without the erased tenant's content is
rewritten, and each rewrite is a model call; a corpus of this size has hundreds. The work
takes longer than the fifteen minutes a function is allowed to live, so the invocation is
cut off in the disposition phase. Nothing is corrupted when that happens, because the phase
marker is durable and an interrupted run is resumable or abortable, but no certificate is
produced.

This is a hosting mismatch rather than a defect in the path, and the distinction is worth
being exact about: the two things the deployment was actually missing are now proven in it —
the signing key signs, and the evidence bucket accepts the write.

Status: the run is performed as the console's execution role from somewhere without a
request timeout, and the role's trust policy now admits a principal of this account
alongside the function. That is not a second signing principal: an operator assuming the
role is the role, so the claim that exactly one role may sign is unchanged, and it is the
same account boundary the key policy and the bucket policy already draw. A run performed
that way completes and issues its certificate, which the console then serves and verifies
like any other.

What remains available and unbuilt is a task-hosted runner. The deployment already has the
cluster, and the image already carries the erasure verb, so the work is a task definition
whose task role is this same console role — which is why the trust policy above is the piece
that had to exist first, and why adding the runner later changes no claim about who may
sign.

### AM. Four grants a deployment needed and the test harness could never want

Six migrations now exist for one reason: the suites apply migrations and assert against them
under an administrative login, and an administrative login holds every privilege there is. A
missing grant is invisible to coverage that is otherwise end to end, and becomes load-bearing
only where a path connects as the narrow role its privileges were written for. `016` through
`020` each closed one instance found that way, one at a time, by a deployment.

So the question was asked properly rather than again: for each of the four service roles,
resolve which modules that path actually imports, extract from those modules every table and
privilege their statements imply, and put each to the deployed cluster **as that role**,
inside a transaction that was rolled back. Three came back refused, and a fourth looked like
a gap and is not one.

The eraser could not delete a `session`. `ledger` references `session` with no action rather
than a cascade, so the sweep removes a session's events and then the session, and it held
`SELECT` and `UPDATE` and no `DELETE`. It therefore removed every event of a session and
stopped on the row that session is — at the very end of the phase that does the deleting.

The watcher could not open its change stream at all, and had never been able to. The stream
is declared over `ledger` and `derived_artifact`, this platform serves a core changefeed only
to a principal holding `SELECT` on every table named in it, and the watcher held the first and
nothing whatever on the second. What makes this one worth reading is how quietly it failed:
the refusal is caught, recorded as an unavailable capability, counted as a metric, and
persisted as the consumption mode, and the timestamp poll takes over. All three records worked
exactly as designed. The watcher ran in its degraded mode, on a cluster that serves the stream
perfectly well, for as long as the statement had existed, and the degradation was reported
faithfully to nobody. `lineage_edge` was the second half of the same mechanism — a derived
artifact carries no session, so one arriving on the stream is attributed by reading the
earliest lineage edge naming one — and granting the stream without it would have moved the
failure one step later rather than removing it.

The apparent fourth is the ingest path's read of the capability record, which the writer is
not granted. That is deliberate and already stated where the read happens: the attempt is
best-effort, a failure is logged at debug and swallowed, and every accessor then reports each
platform fact as unprobed rather than as absent. An empty record is the honest reading when
the role that looked is not allowed to look. Granting it would widen the ingest role for a
value the ingest path is written to do without.

Status: closed. `021_sweep_and_stream_grants.sql` carries all three grants and the reasoning
for each. The scan itself is kept as `tests/integration/test_role_statement_coverage.py`,
which derives the demand from the source rather than restating it in a table: a table of
expected grants would only catch an omission somebody remembered to add to the table, which
is the failure mode above. Each surplus the extraction reports — a shared store module's
write statement that belongs to another path — is recorded there with the reason it is a
deliberate absence, and a second case asserts that none of those absences has quietly become
a grant.

### AN. A cascade that widens what a role needs rather than narrowing it

`020` granted the eraser `SELECT` on the three tables recording a learned procedure's
retrievals, outcomes, and confidence movements, and argued at length for stopping there: each
references its procedure with a cascading delete, so removing the procedure removes them with
it, and a cascade is performed by the constraint rather than by the deleting session, so no
delete privilege is consulted. The grants were right. The argument was false, and the cluster
said so in as many words:

```text
InsufficientPrivilege: while building cascade expression: user molt_eraser_svc does not
have DELETE privilege on relation procedure_retrieval
```

The privilege is consulted while the cascade expression is *built*. The deleting session must
itself hold the delete on every table the cascade will reach, so a cascading reference does
not narrow what a role needs — it widens it, to the transitive closure of the children of
everything the role deletes. The run had by then removed the artifacts it was authorised to
remove and stopped one step from finishing.

Status: closed. `022_eraser_cascade_deletes.sql` grants the delete on all three and carries
the correction, because an applied migration is never edited. Which tables was derived rather
than guessed: every cascading child of every table the erasure path deletes was enumerated
against the cluster, which found five, of which two were already deletable and these three
were not — so the file closes the class rather than the one table the run named first, since
the other two would have failed on the next statement.

The gate for this is a separate case in the same module, and it has to be, because this demand
appears in no statement anywhere. It reads the referential actions out of the cluster and
asserts that a role which may delete a parent may delete everything that cascades from it,
transitively. No amount of reading the source finds this one.

What generalises is smaller than the fix: a claim that the database will do something on a
role's behalf is worth checking against the database. This one read plausibly and was wrong.

### AO. The certificate builder was written, tested, and unreachable

An erasure run completed against the deployed cluster. It reported `status: completed`,
`phase: certificate`, `certificate_admissible: true`, an object key, and exit code zero. No
certificate existed. Not a failed one — none: no row, no object, nothing signed.

The engine says what it does, precisely, in its own docstring: it records a completion and
deliberately assembles no certificate, because a certificate is built from the evidence a run
has committed rather than from the run in progress. It hands that step off. Nothing performed
it. The command-line verb composed the key a certificate *would* have taken and printed it
beside an admissibility flag, which reads exactly like a certificate having been written, and
then returned success.

Two things were missing, and the second is the more interesting. There was no call to the
builder from any production module — the same defect as eighteen console handlers nothing
imported, and as a recall engine never attached to the seam written for it. And there was no
implementation of the object-write protocol anywhere outside the tests: the certificate
surface takes the write as a parameter, which is what lets the whole issuing path run with no
credential, and a seam is only a seam if the deployment has something to pass it. It had the
protocol and nothing satisfying it.

Nothing failed, which is the pattern all three share. The builder's own suites drive it
directly and passed against a module no deployment imported. The erasure suites assert what a
run commits, which was correct. The one claim nobody made was that the two halves were joined.

Two further refusals sat behind those, and both were found only once a real write was
attempted. The evidence bucket refuses a put that does not name its encryption — the template
says so, in the words *an unencrypted write is refused rather than silently encrypted, so a
caller that omits the requirement learns it did* — and this caller omitted it and learned it
did. And applying a retention period on a put is a second action from writing the object, so
the console role could write an unlocked object and not a locked one, which is exactly the
wrong way round for evidence.

Status: closed. `molt/attest/objects.py` implements the object write against the real service,
declaring the encryption the bucket requires; the identity policy grants the retention action
alongside the put; and both erasure paths — the command-line verb and the console's dispatched
worker — issue the certificate from the evidence of a run that has just completed, on the
connection that committed it. A failure to sign or persist now propagates and fails the
invocation, because a run that deleted a tenant's memory and produced no attestation is
something an operator has to be told; a failure to write the object does not, because the
signed document is already in the cluster and the row records that the storage did not
complete. Both failure paths now carry the service's own words rather than an exception class
name, which is what distinguishes a denied permission from a refused retention.

The gate is `tests/quality/test_evidence_paths_wired.py`, and it asserts reachability rather
than behaviour: each module producing evidence a governance claim rests on must be imported by
some production module, and the object protocol must have an implementation outside the
suites. It is deliberately a weak claim. The defect three times running has not been a wrong
artifact but an absent one, and the thing worth testing is the join.

### AP. Verification that refused every connection a deployment can make

With the certificate finally issued, signed, and stored, the last step was to check it the
way the documentation says an auditor checks it. It was refused:

```text
VerificationFailedError: the connection reports no read-only role, or a role that is not
the read-only role, so verification would not carry the no-mutation guarantee it claims
```

The refusal was correct in spirit and wrong in fact. Verification insists on running as the
read-only role, so that the no-mutation guarantee is structural rather than a promise about
the code that follows — a verification cannot write because the role it runs as holds no
privilege to. It established that by reading `current_user` and comparing it against the
role's own spellings, `reader` and `molt_reader`.

A deployment does not log in as a role. It provisions a login per component and grants that
login the role whose privileges were written for it, so the connection authenticates as
`molt_reader_svc`, which *carries* the read-only role and is not named it. The comparison
therefore refused every connection a deployment is capable of making. Certificate
verification could not succeed against the deployed cluster at all.

It passed everywhere it was tested, for the reason this file keeps recording: the suites
connect as an administrator and set the configured label by hand, so both halves of the check
were satisfied by a connection no deployment uses. This is the fifth defect in this document
whose whole cause is that a service role is narrower, or differently named, than the harness.

Status: closed. The cluster's half is now asked as a membership question —
`pg_has_role(current_user, 'molt_reader', 'MEMBER')` — which is the same predicate the
schema's own column guards are written with, so the two cannot come to disagree about what
holding a role means. A login that is the role satisfies membership too, so nothing that
passed before fails now, and a cluster that cannot answer the question is treated as
answering no, because the refusal is the guarantee and an unanswerable check must not become
an admission.

Membership was the right question regardless of the naming. What the guarantee rests on is
the privileges the connection holds, and a name is a claim where a membership is a privilege.

The end-to-end result, for the record: a run against the deployed cluster deleted 620 rows
and retained 257, its certificate was signed under `ECDSA_SHA_256`, persisted in the cluster,
written to the evidence bucket under Object Lock, and then verified from the saved public half
of the key with no call to the key service, over a read-only connection, with every check
passing and none failing.

### AQ. A document every page links to, which the archive did not carry

The footer of every console page links to the Interface_Specification. Following it returned
503 on every request, and the log said why: `FileNotFoundError`. The packaging script stages
the console's templates and stylesheet — it carries a comment explaining, at length, that
their absence once left every page answering that its templates were unavailable — and it did
not stage the specification document beside them.

So this is the same omission as that one, in a second place, and with the same signature: the
archive existed, both function stacks were created, the handler imported, and the failure
arrived per request on one route. The route's own code was correct and its docstring even
anticipates the state, describing an absent document as a 503 naming no path because the path
is a deployment fact. It was right about that. The deployment fact was that nothing put the
file there.

Status: closed. The document is staged at the same place relative to the archive root that it
occupies relative to a checkout's, which is how the templates are staged and how the console
resolves both.

### AR. A diagram where two thirds of the nodes linked to nothing

Every node of the lineage diagram was drawn as a link to its own subgraph. Only a
Derived_Artifact has one. The closure that builds the graph deliberately reaches nodes that
are not Artifacts — the Event that produced one, the Session it happened in — because
omitting them would draw an edge to nothing, and those nodes were linked to a route that
answers `no such Artifact`. A reviewer exploring the provenance graph by clicking it found a
404 more often than not.

Status: closed. Whether a node has a page of its own is now a property of the node rather
than a condition in the template, because the graph renders each node in three places and a
link condition written three times is one that comes to disagree with itself. A node that is
not a link is still focusable and still carries its accessible name, so nothing is lost to a
keyboard or to a screen reader.

### AS. A fallback that ran because nothing ever asked for the primary

The Policy_Watcher's change stream is its primary mechanism and the timestamp poll is its
documented fallback. Every deployment ran the fallback. This document recorded that as
verified, and the README stated it as a property of the cluster's plan: *the sinkless change
stream is not served on this tier*.

The cluster serves it. Probing the deployed cluster as the watcher's own role, with the
watcher's own statement including both of its options, was permitted on the first attempt and
on every variant tried.

`Watcher.from_configuration` defaulted its stream opener to `None`. A watcher holding no
opener raises `ChangefeedRejectedError` from `_open_stream` **before** any connection is
opened — the message was *no streaming connection was supplied to the watcher* — and that
refusal is caught by the same handler that catches a refusal from the cluster. The handler
recorded the `changefeed` capability unavailable, counted a degradation, persisted `polling`
as the mode, and logged a fixed sentence: *the cluster refused the sinkless change stream*.
`dedicated_opener` existed, was correct, and was called by nothing outside the tests.

Three things kept it invisible, and each is a pattern worth naming rather than an accident.

The fallback works, so nothing failed. Policy propagated, the kill switch held its bound, and
the health route answered honestly about the mode it was in.

The refusal was reported through all three channels a real refusal uses — a capability row, a
metric, a log record — so the reporting looked like a system correctly observing its own
environment. This is the same shape as finding AG, where a placeholder answered every console
route with a well-formed *not implemented*: a reasonable answer to one question is a wrong
answer to all of them at once, and nothing distinguishes the two if the reporting is uniform.

And the reason was a sentence written at the catch site rather than the refusal's own message,
so every cause arrived identically. A cluster that will not serve the statement, a connection
that could not be opened, and an opener that does not exist are three different problems with
three different repairs, and they were one string.

Status: closed, and in four parts. `MemoryStore.open_dedicated` opens a connection outside
the pool, which is what a stream that never returns requires and why a pooled one would strand
the pool behind a cursor that by design cannot finish. `store_opener` builds an opener over it
and is now the default `from_configuration` supplies, so the primary mechanism is the one a
deployment gets. The refusal now carries the underlying cause, and the degradation reason is
the refusal's own message rather than a sentence about the cluster. Deployed: the watcher
records `changefeed` available, its watermark reads `changefeed`, and it logs *consuming
mutations from the sinkless change stream*.

The gate is `tests/unit/test_watcher_stream_wired.py`, and it asserts the join rather than the
behaviour: a watcher built the way a deployment builds one holds an opener, and calling that
opener reaches the store for a connection. Neither needs a cluster, which is the point — this
defect was never visible from one, and four cases of it have now been found in this codebase
where the missing thing was a default or an import rather than any logic at all.

What generalises: when a system reports that its environment lacks a capability, the report is
a claim about the environment made by the code, and it is worth checking against the
environment directly. Ask the cluster yourself. This one had been believed for as long as the
module existed, was written into the documentation as a property of the vendor's plan, and was
wrong.
