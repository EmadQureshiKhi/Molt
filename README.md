<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/molt-logo-dark.png">
    <img src="assets/molt-logo-light.png" alt="Molt" width="520">
  </picture>
</p>

<h1 align="center">Molt</h1>

<p align="center">
  <strong>Durable, distributed, governable memory for AI coding agents,<br>
  with erasure a hostile reviewer can verify against the live cluster.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-MIT-111111" alt="MIT licensed">
  <img src="https://img.shields.io/badge/python-3.11%2B-111111" alt="Python 3.11 or newer">
  <img src="https://img.shields.io/badge/database-CockroachDB%20Cloud-111111" alt="CockroachDB Cloud">
  <img src="https://img.shields.io/badge/cloud-AWS-111111" alt="AWS">
  <img src="https://img.shields.io/badge/vector-1024%20dims-111111" alt="1024 dimension vectors">
</p>

A managed CockroachDB cluster is the single system of record: every agent session,
derived artifact, provenance edge, embedding, and piece of erasure evidence is a
relational row in one cluster. Nothing important lives on an engineer's machine, and no
governance claim is made that cannot be re-derived from the cluster by someone who does
not trust the people running it.

The headline capability is **provable forgetting**.

## Contents

- [Why this exists](#why-this-exists)
- [Architecture](#architecture)
- [The write path](#the-write-path)
- [The erasure path](#the-erasure-path)
- [Semantic residue and the threshold decision](#semantic-residue-and-the-threshold-decision)
- [Erasure ownership and run state](#erasure-ownership-and-run-state)
- [Data model](#data-model)
- [Attribution as a version history](#attribution-as-a-version-history)
- [Six memory tiers](#six-memory-tiers)
- [Trust boundaries](#trust-boundaries)
- [Deployment topology](#deployment-topology)
- [CockroachDB: what each capability is used for](#cockroachdb-what-each-capability-is-used-for)
- [AWS: what each service is used for](#aws-what-each-service-is-used-for)
- [Model providers](#model-providers)
- [Demonstration](#demonstration)
- [Running it locally](#running-it-locally)
- [Verification](#verification)
- [Repository layout](#repository-layout)
- [Documentation](#documentation)
- [Status](#status)

## Why this exists

A consultancy runs AI coding agents across many client codebases. Every session becomes
shared memory that agents on any machine can read, so that memory is a shadow copy of
each client's source, secrets, and internal architecture. Worse, it *blends* clients:
behavioural baselines and learned procedures are distilled from work across several
engagements at once.

When one engagement ends under a contractual purge obligation, someone has to be able to
say the client is gone, and be believed by a reviewer who is paid to disbelieve them. Two
things make that hard, and they are the reason Molt exists rather than a
`DELETE ... WHERE client_id = ?`.

**Semantic residue.** Proprietary code pasted out of one client's codebase into a session
on another client's repository carries no repository path, no ticket number, no identifier
of any kind. Exact-match search cannot find it. The only thing that can is vector
similarity over embeddings of the content itself, which means erasure has to include a
search returning *probable* matches and then commit to a decision about each one, with the
borderline band decided by a model call that fails closed toward inclusion and every
decision recorded as evidence.

**Blended derived artifacts.** A behavioural baseline distilled from three clients must
lose one client's contribution while remaining valid for the other two. Deleting it
destroys memory the two remaining clients paid for. Keeping it is a breach. So the
artifact is surgically rewritten, the rewrite is validated against the erased client's
markers before it is accepted, and a rewrite that fails validation degrades to a hard
delete rather than silently passing.

Memory is also on the agent's critical path rather than beside it. Before acting, an agent
asks memory what happened the last time anyone in the fleet tried something similar, and
the recorded outcomes change what it does next. That is what makes stale or unerased
memory a live problem rather than a storage problem.

## Architecture

![Molt architecture. Capture on the engineer machine redacts and signs Events and posts
them to the Collector on Lambda. Ingest and recall components, an erasure engine running
under a fenced lease, and the evidence components all read and write one CockroachDB Cloud
cluster holding six memory tiers. A policy watcher on Fargate consumes the write stream as
a changefeed. Certificates are signed with a KMS key and stored in S3 under Object Lock,
and an untrusted auditor reads client filtered views through the cluster's managed MCP
endpoint.](assets/molt-architecture.svg)

The delivered vertical slice is **capture → ledger → semantic recall → policy watcher →
erasure certificate**.

| Stage | What happens |
|---|---|
| **Capture** | A hook, an MCP proxy, or a decorator turns agent activity into Events on the engineer's machine, redacts secret material before anything leaves the process, signs the batch, and posts it over HTTPS. It holds no database credential and exits successfully in every branch, so a capture failure never breaks an agent run. |
| **Ledger** | The Collector verifies the signature and the request age, then appends Events inside one serializable transaction. Sequence numbers and hash-chain digests are computed *inside* the inserting statement, so no process ever reads a digest and writes it back. Attribution to a client is detected at ingest and stored as an immutable version history. |
| **Semantic recall** | Content is embedded through a provider interface and stored in a `VECTOR(1024)` column served by a distributed vector index. Recall answers the agent's pre-action query under a tenancy filter applied inside SQL, and learned procedures carry a confidence value that rises on success and falls on failure. |
| **Policy watcher** | The write stream is consumed as a changefeed and evaluated against declarative rules whose actions run from advisory warning up to halting a session, so a policy breach can stop an agent rather than merely annotate it afterwards. |
| **Erasure certificate** | Erasure runs under a fenced lease in three phases, writing evidence before, during, and after mutation. The certificate is assembled from those stored rows, canonicalised, signed with a key held outside the cluster, and shipped with SQL a third party can run against the live cluster to re-confirm its central claim. |

Four decisions carry most of the load, and every structural oddity follows from one of
them:

1. **The cluster is the truth, not a mirror.** There is no local event log shipped into a
   database later. The only local persistence is a bounded retry spool.
2. **Tamper evidence is produced by the writing statement.** Nothing reads a digest in a
   separate round trip and writes it back, because that gap is where a concurrent writer
   breaks the chain and a determined one forges it.
3. **Nothing on an engineer machine holds database credentials.** Capture and recall both
   speak to the Collector over HTTPS with a bearer token and a keyed request signature.
4. **Governance claims are protected structurally.** The enforcement point is a
   constraint, a privilege, or a key rather than an application rule.

## The write path

![Molt write path sequence. An agent tool invokes capture, which maps activity to Events,
redacts them in process, signs the batch, and posts it to the Collector. The Collector
compares the token in constant time, checks the request age, then inside one serializable
transaction upserts the session, inserts ledger rows whose sequence numbers and digests
are computed by the inserting statement, supersedes attribution, and inserts pending
embedding rows. Vectors are written in a second transaction and the policy watcher
consumes the write stream as a changefeed.](assets/molt-write-path.svg)

Three details matter more than they look.

Redaction happens **before transmission**, on the machine, so secret material never
becomes a network payload and never depends on the Collector behaving well. The redacted
flag travels with the row so a reader knows the content is not verbatim.

The digest chain is computed **in the inserting statement**, which is why the ledger tier
can be append-only with no role holding `UPDATE`.

Embedding is a **second transaction**. The ingest transaction inserts a placeholder in a
pending state and returns, because a provider call inside the capture path would put a
third-party latency budget in front of an agent.

## The erasure path

![Molt erasure path sequence. An operator starts a run, the lease manager grants one
fenced lease per client at the next generation, and the backup manager secures pre erasure
evidence before any mutation. Under the held lease the engine runs an explicit sweep,
semantic residue detection with a fail closed adjudication band, and per artifact
disposition. The certificate is then assembled from stored evidence rows, signed with a
key held outside the cluster, and stored under Object
Lock.](assets/molt-erasure-path.svg)

**Evidence is written before, during, and after mutation.** The run row exists with its
before boundary before anything is deleted, each artifact's decision becomes a durable
disposition row as it is made, and the certificate is assembled by *reading those rows
back* rather than from the process's own memory. A run that crashes halfway leaves a
readable account of how far it got.

**Every phase fails in the safe direction.** No lease means abort before mutation. A
failed backup means abort before mutation. An unavailable adjudication means include the
candidate. A rewrite that fails validation means hard delete the artifact.

**Ownership is fenced, not merely locked.** The lease carries a monotonically increasing
generation, a partial uniqueness constraint admits one current lease per client, and every
erasure write carries the generation it believes it holds. A superseded worker has its
writes refused by the database with a stale-generation error rather than overwriting the
work of the worker that replaced it.

The disposition decision, made in SQL over the candidate set joined to current attribution:

| Condition | Disposition | What happens |
|---|---|---|
| No other client is bound to the artifact | `hard_delete` | The artifact, its embeddings, its lineage edges in both directions, and its bindings are deleted, with the pre-deletion digest captured first |
| Another client is bound, and the artifact is derived content | `surgical_redaction` | The body is rewritten, validated, and replaced under an optimistic digest guard; the erased client's binding is closed as history; both digests and a structural diff summary are recorded |
| Another client is bound, but the row is an Event or a Session | `hard_delete` | An Event body cannot be partially attributed, so the reason recorded is `event_not_divisible` |
| Adjudication excluded it, or its binding no longer exists | `retained` | A reason is recorded and nothing is mutated |

## Semantic residue and the threshold decision

![Molt semantic residue decision bands. A chart of cosine distance from a query artifact
divided into three bands by two thresholds: the nearest candidates are included with no
model call, the middle band is adjudicated by a text model that fails closed toward
inclusion, and the far band is excluded with its distance and reason still recorded.
Planted ground truth fragments are marked, and a twenty five pair sensitivity grid below
shows how each threshold pair would score.](assets/molt-residue-bands.svg)

Two thresholds set erasure scope, and they are the only tuning decision in the system that
changes what gets deleted. So they are not chosen by feel. The sensitivity analyzer runs
one search at the widest review threshold, retains every candidate with its distance, then
counts every grid pair against that retained set. It connects with the reader role and
opens no write transaction, so its purity is a privilege fact rather than a discipline, and
it calls the text model for no candidate at all.

A pair whose auto-inclusion threshold exceeds its review threshold is reported as
inapplicable with that reason rather than skipped, so the grid stays rectangular. Raising
either threshold can only widen the candidate set, which is the monotonicity a property
test asserts over randomised grids.

## Erasure ownership and run state

![Molt erasure ownership and run state. Two state machines. The lease lifecycle runs from
no lease, to held at a generation, through expiry judged on the cluster clock, to takeover
at the next generation, leaving the previous holder superseded so its writes are refused as
stale. The run lifecycle runs from requested through swept, residue found, disposed,
finalised, and certified, with an aborted state reachable from any point that keeps the
evidence already written.](assets/molt-lease-states.svg)

A lease is not a lock. It carries a generation, the generation travels on every erasure
write, and the database refuses a write from a generation that has been superseded. That
distinction is what a concurrency test demonstrates with ten real worker processes: one
grant, nine refusals each naming the current owner, the winner killed without releasing,
takeover only once the expiry has passed on the cluster's clock, and the revived zombie's
write refused with both generations named and no row persisted.

## Data model

![Molt data model by memory tier. Twenty nine tables grouped by the tier that owns them:
identity and session, the append only episodic ledger, the attribution version history,
revisable procedural and semantic memory, the vector index, immutable provenance including
signed checkpoints, write once erasure evidence, policy state, and the disposable working
tier. Beside the tables are the relationships that carry the guarantees, the foreign keys
deliberately absent, and what the four database roles
enforce.](assets/molt-data-model.svg)

Twenty nine tables, fifteen migrations, two generations. The first generation, `001`
through `007`, lays down the core ledger, derived artifacts and lineage, embeddings and
the vector index, erasure evidence, policy, row-level expiry, and roles. The second,
`008` onward, adds bitemporal attribution, erasure leases and fencing, signed checkpoints,
the working tier, confidence-weighted procedural memory, structural protection of the
audit record, the grants those tables need, and the structural diff summary a surgical
redaction records on its disposition row.

A correction to an applied migration is always a new numbered file, never an edit, because
the runner records a digest per file and refuses to run when a recorded digest no longer
matches.

## Attribution as a version history

![Molt attribution version history. A timeline of one artifact's client attribution across
validity time: a first version opened at ingest by workspace mapping, superseded by a
second version detected by vector similarity with higher confidence, and finally closed by
an erasure that records the removal as history rather than leaving a hole. Beside it are
the constraints that keep the history honest and the as of query the certificate
reads.](assets/molt-attribution-timeline.svg)

A mutable row answers what is true now. An auditor asks something else: how long did you
hold this, and how did you work out it was ours. So attribution is an append-only version
history. A stored version's claim is immutable, and only its validity end and its
superseding reference are ever written, each exactly once, under a privilege scoped to
those two columns alone.

That is also why a sweep resolves against current versions only, so a superseded version
can neither widen nor narrow the erasure, and why the certificate can carry the earliest
validity start per touched artifact, which is a claim a deletion receipt cannot make.

## Six memory tiers

Every stored row belongs to exactly one tier. The tiers are not labels applied after the
fact: a tier exists because its rows carry a different **mutability contract** and lean on
a different **database capability** to hold that contract. Those two facts together are
what make the cluster the agent's memory rather than its logfile.

| Tier | Holds | Mutability contract | Capability it leans on |
|---|---|---|---|
| `episodic` | `ledger` | Append-only. No role holds `UPDATE`; rows leave only by an authorised erasure or by expiry | Serializable isolation, so ordering and digests are assigned by the writing statement |
| `attribution` | `client_binding` as a version history | Append-only with closure. A stored version's claim is immutable; only its validity end and superseding reference are ever written, each exactly once | Serializable isolation, so closing the old version and opening the new one commit as one supersession |
| `procedural_semantic` | `derived_artifact`, the procedure tables | Revisable. Bodies are surgically rewritten and confidence moves with recorded outcomes, always with a change record beside it | Distributed vector index for recall, plus a column-scoped grant confining what a revision may touch |
| `provenance` | `lineage_edge`, the chain columns, `ledger_checkpoint` | Immutable. Edges are inserted and deleted, never edited; chain columns are never rewritten | Recursive queries for lineage closure, digests computed in-statement, referential actions refusing deletions that would erase history |
| `action` | The erasure evidence tables | Write-once evidence, apart from the lease that governs who may write it | A partial uniqueness constraint admitting one current lease, and restrictive referential actions protecting the evidence chain |
| `working` | `working_memory` | Disposable. Overwritten freely, physically deleted on expiry, referenced by nothing | Row-level expiry enforced by the cluster rather than by an outside scheduler |

The taxonomy is encoded once, in `src/molt/models/tiers.py`. The console tier view and
[`docs/memory-tiers.md`](docs/memory-tiers.md) both read that mapping, so the design, the
rendered view, and the documentation cannot state three different taxonomies.

## Trust boundaries

![Molt trust boundaries. Eight zones with the crossing between each pair labelled: three
untrusted zones for the engineer machine, the public visitor, and the auditor; the role
scoped server side; the cluster, which trusts nothing above it; the key service; the model
providers; and evidence storage. Beside them are the residues accepted rather than
mitigated.](assets/molt-trust-boundaries.svg)

Three zones are untrusted on purpose, and the auditor is one of them. That is the whole
posture: a reviewer acting for a departing client is given read-only, per-client-filtered
access to the cluster itself rather than a document to take on faith.

Two residues are stated rather than glossed. The hash chain and the signed checkpoints give
tamper **evidence**, not tamper proofing; what the checkpoint adds is coverage of every
session in a window and detection by a party who does not trust the cluster administrator,
because the key lives outside the cluster. And the request signature **bounds** the
replayable window to the configured maximum request age rather than eliminating replay. A
per-request nonce table would close it and would also put a write-contended row in front of
every capture, so the residue is accepted deliberately. The full accounting, including the
seven named threats, is in [`docs/threat-model.md`](docs/threat-model.md).

## Deployment topology

![Molt deployment topology. Every provisioned resource in one region: a CloudFront
distribution in front of a console function URL, a collector function URL under a reserved
concurrency ceiling, two Lambda functions, two Fargate services in public subnets with zero
inbound rules, and the managed services for parameters, keys, evidence storage, telemetry,
and models. The cluster and the model providers sit outside the account, and the four
deliberately absent resources are listed with the reason for
each.](assets/molt-deployment.svg)

## CockroachDB: what each capability is used for

| Capability | What Molt does with it | Where |
|---|---|---|
| **Serializable isolation** | Assigns ledger sequence numbers and chain digests inside the inserting statement; makes attribution supersession and fencing-generation assignment atomic rather than racy | `store/chain.py`, `store/attribution.py`, `erase/lease.py` |
| **`VECTOR` column and distributed vector index** | Stores unit-normalised embeddings and serves both agent recall and semantic residue detection. The index reports the L2 operator class, so normalising at write time is what keeps cosine thresholds meaningful. The exact scan remains only as a fallback | `migrations/003_embedding.sql`, `recall/`, `erase/residue.py` |
| **`JSONB` and `TIMESTAMPTZ` native types** | Event payloads and validity intervals are queryable columns rather than opaque blobs | `migrations/001_core.sql`, `migrations/008_attribution.sql` |
| **Recursive queries** | Walk the lineage graph to find every artifact derived from a client's data, with a cycle guard | `store/lineage.py` |
| **Row-level expiry** | Makes the working tier genuinely disposable, swept by the cluster | `migrations/006_retention.sql`, `migrations/011_working.sql` |
| **Changefeeds** | Feed the policy watcher the memory write stream; timestamp polling from a stored watermark is retained as a fallback | `policy/watcher.py` |
| **Historical reads** | Corroborate certificate counts when both run boundaries still fall inside the garbage-collection horizon, measured at 4500 seconds | `store/historical.py`, `attest/builder.py` |
| **Roles, privileges, referential actions** | Enforce append-only tiers, least-privilege components, and refusal to delete audit evidence, at the database rather than in application code | `migrations/007_roles.sql`, `013_protection.sql`, `014_grants.sql` |
| **Cluster backups** | Secure pre-erasure evidence before the first mutation of a run, with a managed-backup reference as the fallback | `backup/__init__.py` |
| **Managed MCP endpoint** | Gives an untrusted auditor read-only, per-client-filtered interrogation access | `scripts/provision_roles.sh`, [`docs/auditor.md`](docs/auditor.md) |
| **ccloud CLI** | Provisions the cluster, roles, and service accounts, probes platform behaviour rather than assuming it, and lists managed backups at runtime for the fallback path | `scripts/provision_cluster.sh`, `scripts/probe_capabilities.py` |
| **Agent Skills** | Schema and query review while writing the migrations and the hot queries, and the open format the three shipped skills conform to | `skills/`, [`docs/skills.md`](docs/skills.md) |

Four platform facts were **probed live and recorded in a capability table** the store reads
at process start. No component branches on a version string.

| Fact | Result | Consequence |
|---|---|---|
| Distributed vector index | Available, reported with the `vector_l2_ops` operator class | Index-served recall and residue detection; every vector unit-normalised at write time |
| Sinkless changefeeds | Permitted, rangefeeds enabled | Changefeed consumption is the watcher's expected mode, and its health route reports which mode it is in |
| Garbage-collection horizon | 4500 seconds | Certificate counts are derived from the ledger and stored dispositions, which expire never; the historical read is demoted to opportunistic corroboration |
| On-demand backup in the cloud control plane | Absent; listing and configuration only | `BACKUP INTO` an operator-owned bucket is the primary path, and a managed-backup reference is marked *referenced* rather than *taken* |

## AWS: what each service is used for

| Service | Role | Declared in |
|---|---|---|
| **Lambda** | The Collector ingest function and the console, each behind an HTTPS function endpoint, so there is no idle server in the capture path. The Collector is a plain handler with no framework, under a reserved concurrency ceiling so a leaked token has a bounded cost | `infra/templates/collector.yaml`, `console.yaml` |
| **ECS Fargate** | The two components that hold long-running connections: the policy watcher, which holds a changefeed cursor, and Molt's own MCP server, which holds client sessions. Public subnets, security groups with zero inbound rules, no NAT gateway | `watcher.yaml`, `mcp.yaml`, `network.yaml` |
| **CloudFront** | Terminates HTTPS for the console on its own default certificate and generated hostname, which removes a load balancer, an ACM certificate, and a custom domain from the design | `cdn.yaml` |
| **KMS** | One asymmetric key signs erasure certificates and ledger checkpoints. Verification retrieves the public key and checks locally, so verifying survives losing permission to call the service. The sign permission sits on exactly one role | `kms.yaml` |
| **S3** | Certificates in a versioned bucket with Object Lock enabled at creation, a policy denying unencrypted writes, and the object key and version recorded on the certificate row | `storage.yaml` |
| **Parameter Store** | Every secret, standard tier: a connection string per database role, the ingest token, the ingress signing secret, the console credential, and provider credentials | `parameters.yaml` |
| **CloudWatch** | Metrics under a cardinality bound with overflow diverted to structured logs, and the erasure run identifier as the correlation identifier across a run | `observability.yaml` |
| **CloudFormation** | Ten templates deployed in order by a wrapper that validates parameters, torn down in reverse by one that releases Object Lock retention first | `infra/deploy.sh`, `infra/teardown.sh` |
| **IAM** | Least privilege per component, with no wildcard resource outside a metric statement conditioned on the namespace | every template |
| **Bedrock** | The documented default provider implementation for both the embedding and the text role | `providers/bedrock.py` |

Deliberately absent, each for a stated reason: no Application Load Balancer, because no
certificate can be issued for its own generated hostname and it bills hourly regardless.
No NAT gateway and no interface endpoints, because outbound-only tasks in public subnets
with no inbound rules do the same job. No per-secret secret store, because at this secret
count the monthly per-secret charge would dominate everything else.

## Model providers

Model access always goes through an `EmbeddingProvider` or `TextProvider` interface, never
a vendor SDK directly, so which provider answers is a configuration concern rather than an
architectural one.

| Role | What calls it | Contract |
|---|---|---|
| Embedding | The Embedder, and residue detection through it | Returns exactly 1024 dimensions. The selector probes at startup and refuses any other width before a single vector is written |
| Text | The Adjudicator and the Redaction_Rewriter | One call per borderline candidate or blended artifact. Both callers fail closed: an unavailable adjudication includes the candidate, and a rewrite that fails validation becomes a hard delete |

Bedrock is the documented default for both roles. The delivered demonstration
configuration selects external implementations, because on-demand inference quota was zero
and not adjustable on the account used. That is exactly the situation the interface exists
for: the fix was a configuration line rather than a rewrite of three components. See
[`docs/providers.md`](docs/providers.md).

Adjudication prompts are split into a stable prefix and a variable suffix so a provider
supporting prompt caching turns the shared task instruction and query excerpt into one
paid write and many cheap reads. The cache boundary is only marked above a measured
minimum prefix length, because a cache write with no subsequent read costs more than no
caching.

## Demonstration

> **Placeholder.** The deployment is pending, so the URL and the recording below are not
> yet published. Everything in this section describes the walkthrough the console is built
> to serve, and each step is reachable in read-only demonstration mode with no credential.

| Item | Where |
|---|---|
| Live console | `<DEMO_URL>` — the CloudFront generated hostname |
| Recorded walkthrough | `<VIDEO_URL>` |
| A signed certificate to read without deploying anything | `<CERTIFICATE_PATH>` |
| Auditor access | `<MANAGED_MCP_ENDPOINT>` with a per-client read-only service account, following [`docs/auditor.md`](docs/auditor.md) |

The walkthrough, in the order a reviewer should see it:

1. **Fleet view.** Sessions across several clients, with per-session event streams and the
   chain verification status of each.
2. **Lineage view.** The graph of what was derived from what, filterable, with an
   equivalent edge-list table beside the diagram rather than only a picture.
3. **Recall.** The same query an agent makes before acting, showing a confidence-weighted
   learned procedure and the recorded outcomes that came with it.
4. **Residue view.** Cross-client fragments found by vector similarity alone, each with
   its distance, its band, and the reason it was included or excluded.
5. **Sensitivity grid.** What each threshold pair would include, refer for adjudication,
   and recover against planted ground truth. Read-only, and it calls no model.
6. **Erasure run.** Started for one client, streaming its phases from the durable phase
   marker rather than from process memory, so a reconnecting client still sees the truth.
7. **Redaction comparison.** A blended artifact after surgical rewrite: both digests, the
   binding sets before and after, the structural diff summary, and an explicit statement
   that the original text was not retained.
8. **Certificate.** The signed document, its ownership generation, its first-attribution
   field, the checkpoint it names, and a live verification that runs its embedded SQL
   against the cluster.
9. **Lease contention.** Ten real worker processes contending for one erasure lease, one
   winner, and a revived zombie worker whose write is refused with a stale generation.

Demonstration mode establishes an anonymous read-only principal restricted to the seeded
clients, and every mutation route answers 403 by route name before its handler runs. A
completed seeded run is replayed through the same streaming view, so phase streaming, the
redaction comparison, and certificate verification stay observable without exposing a
mutation route to the internet.

## Running it locally

Everything below runs on a checkout with no cloud account and no cluster.

```text
python3.12 -m pip install -r requirements.txt
```

The package is importable from `src/` with no install step, because the test configuration
puts it on the path.

### A local database

`scripts/run_local_db.sh` manages a single-node insecure local instance for the
database-backed suites and prints its connection string on standard output. It listens on
the loopback interface and carries no password, so nothing it prints is a secret. Every
action is idempotent.

```text
scripts/run_local_db.sh start     # start if needed, print the connection string
scripts/run_local_db.sh status    # report running or not
scripts/run_local_db.sh stop      # stop, keeping the data directory
scripts/run_local_db.sh wipe      # stop and remove the state directory
```

### Migrations

Applied in ascending order by a runner that records what it ran and refuses to reapply it.
The connection string resolves through the configuration surface rather than through an
argument:

```text
export MOLT_DSN="<connection string printed by run_local_db.sh>"
python3.12 -m molt.store.migrate
```

### Operator commands

The `molt` argument tree covers every operator workflow, with exit codes separating
operational failure, usage error, and a verification outcome of failed:

```text
molt seed                     # multi-client seed data, including planted contamination
molt recall --query "..."     # the query an agent makes before acting
molt residue --client acme    # residue search in read-only mode, mutating nothing
molt sensitivity              # the threshold grid, under the reader role
molt erase --client acme      # a leased run, with a dry-run mode
molt attest verify <path>     # signature, embedded SQL, chain, and checkpoint
molt verify-chain --session …  # recompute every digest for one session
molt contend                  # the lease contention demonstration
molt watch                    # the policy watcher, with a single-batch mode
molt serve                    # the console
molt mcp                      # the read-only tool server
molt retention                # the retention report
```

## Verification

The suites are separated by what they *need*, not by what they measure, and the separation
is enforced by markers.

| Suite | Needs |
|---|---|
| `tests/unit`, `tests/property`, `tests/quality`, `tests/spec` | Nothing. A bare checkout runs them |
| `tests/integration`, `tests/concurrency`, `tests/e2e`, `tests/skills` | A reachable database instance, from `MOLT_TEST_DSN` or `MOLT_DSN` |
| `tests/perf` | A reachable instance where a test measures the cluster's own work |
| Anything marked `services` | Cloud and model provider credentials |

The credential-free run, which is exactly what the workflow does:

```text
python3.12 -m pytest tests/unit tests/property \
  -m "not integration and not services and not concurrency and not e2e and not perf"
```

**Forty correctness properties** are specified in the design, each implemented as one
Hypothesis test with a generator behind it. They assert the things a demonstration cannot
show: that a dry run and a residue sweep leave a byte-identical digest across every
memory-content table, that no current attribution for an erased client survives a run,
that a surgical rewrite leaves the other clients' bindings exactly as they were, that a
certificate canonicalises to identical bytes under key and array shuffling, that altering
any single byte of a signed payload is detected, and that recall never crosses a tenancy
boundary.

Five gates run before any suite, with the same invocations locally and in the workflow:

```text
python3.12 -m mypy
python3.12 scripts/check_type_ignores.py
python3.12 -m ruff check src/molt tests scripts infra
python3.12 -m ruff format --check src/molt tests scripts infra
python3.12 scripts/hygiene.py
```

The type-ignore gate fails in both directions, so a stale allowlist entry is an error too.
The hygiene gate asserts that no tracked source or documentation file carries attributable
metadata across eight pattern classes: no personal name, no date, no timestamp, no
version-history entry, and no identifier of material studied while building. See
[`docs/hygiene.md`](docs/hygiene.md) and [`docs/typing.md`](docs/typing.md).

## Repository layout

```text
src/molt/
  config/      resolution order, secrets accessor, capability record
  models/      Events, sessions, derived artifacts, bindings, the tier taxonomy
  capture/     hook entry point, spool, one adapter per agent tool, proxy, decorator
  redact/      pattern set and recursive redaction
  store/       Memory_Store, migrations, chain, lineage, attribution, working, fencing
  collector/   ingest handler, routing, signed ingress, body bound
  providers/   the two protocols, registry, selector, and the implementations
  embed/       batching, normalisation, pending drain
  recall/      the agent critical path read
  erase/       engine, lease, sweep, residue, sensitivity, adjudicator, rewriter
  confidence/  retrievals, outcomes, adjustment, history
  attest/      certificate assembly and verification, checkpoints, the canonicaliser
  policy/      watcher, rules, kill switch, approvals
  retention/   expiry configuration and reporting
  backup/      the backup statement path and the managed-backup reference fallback
  mcpserver/   tool registry and both transports
  console/     the demonstration application
  seed/        generator, corpora, contamination planting
  telemetry/   metrics, structured logs, correlation
  cli/         the argument tree, one module per verb
tests/         unit, property, integration, concurrency, e2e, perf, security, and more
skills/        three agent skills in the open format
infra/         ten templates, parameters, deploy and teardown wrappers
scripts/       provisioning, local database, capability probes, the hygiene gate
web/           templates and static assets for the console
assets/        the logo and the four rendered diagrams
docs/          the documentation set below
```

## Documentation

| Document | Covers |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Component inventory, the write path, the erasure path, trust boundaries, and the four probed platform facts |
| [`docs/setup.md`](docs/setup.md) | Running it locally, and what provisioning and deployment require |
| [`docs/platform.md`](docs/platform.md) | Every measured platform fact, and what each one changed |
| [`docs/memory-tiers.md`](docs/memory-tiers.md) | The six tiers in full, generated from the single encoded taxonomy |
| [`docs/protection.md`](docs/protection.md) | Why deleting a row of audit evidence is refused by the database |
| [`docs/threat-model.md`](docs/threat-model.md) | Trust boundaries, seven named threats, each mitigation, and the residues accepted on purpose |
| [`docs/providers.md`](docs/providers.md) | The provider abstraction, the default, and the delivered selection per role |
| [`docs/mcp.md`](docs/mcp.md) | The read-only tool surface, both transports, and the permitted client set |
| [`docs/auditor.md`](docs/auditor.md) | How an untrusted reviewer interrogates the cluster directly |
| [`docs/skills.md`](docs/skills.md) | The three shipped agent skills and how a client loads them |
| [`docs/interface.json`](docs/interface.json) | Every route and every tool, with request shape, response shape, authentication, and error responses |
| [`docs/traceability.md`](docs/traceability.md) | Requirement to implementation to test, one row at a time |
| [`docs/reviews.md`](docs/reviews.md) | Schema and query review notes |
| [`docs/glossary.md`](docs/glossary.md) | Every domain term, component name, and external service name, defined once |
| [`docs/hygiene.md`](docs/hygiene.md), [`docs/typing.md`](docs/typing.md) | The provenance gate and the static-analysis gates |
| [`docs/hooks/`](docs/hooks) | Per-tool hook specification notes for the five supported agent command-line tools |

## Status

Honest accounting, because the interesting claims here are governance claims and an
overstated one is worse than a missing one.

**Built.** The capture layer across all five supported agent tools, with its spool and its
signing. The Collector with signed, age-bounded ingress. Both migration generations and
the `Memory_Store` data-access layer, including the attribution, working-tier, historical
read, and fencing modules. The embedder, the binding detector, the recall engine, and the
confidence tracker. Lease grant, refusal, renewal, takeover, and idempotent finalisation,
with fenced writes on the evidence paths. The three erasure phases and their orchestration,
the adjudicator and the rewriter with their fail-closed paths, and the backup manager with
both of its paths. The sensitivity analyzer. Certificate assembly, signing, storage, and
independent verification. Checkpoint computation, signing, and verification with accounted
and unaccounted disagreement. The policy watcher, its changefeed consumption and polling
fallback, the kill switch, and the approval queue. The retention manager. Molt's own MCP
server and the three shipped skills. The seed generator. The console and the command-line
interface. The provisioning scripts with their capability probes, the ten infrastructure
templates with their deployment wrappers, the machine-readable interface specification,
and the documentation set.

**In flight.** Deployment of the stacks and the first end-to-end signed certificate
produced against the live cluster, telemetry integration across the remaining components,
and the last of the end-to-end and service-credential test coverage. Until those land,
read the demonstration section as the walkthrough the console is built to serve rather than
as a published URL.

Licensed under the MIT terms recorded in [`LICENSE`](LICENSE).
