# Cost

The delivered demonstration configuration runs under a stated maximum monthly cost,
and this document names it, names every service behind it, and — the part that
actually matters — says for each figure whether it was **measured**, **derived**, or
**estimated**. A guessed number presented as a measurement is the one thing that
would discredit the rest, so the label is attached to every figure rather than to the
document as a whole.

| Label | What it means here |
|---|---|
| **Measured** | Read back out of a running system by a command recorded in this document, at volumes this document states |
| **Derived** | Computed from a measured quantity and a structural fact about the code, with the arithmetic shown |
| **Estimated** | A judgement about consumption or a published unit price. Not measured, and re-checkable only against the vendor's current price list |

**Maximum monthly cost: US$30, estimated.** The estimate is built in the table below
and it is dominated by two fixed line items: two Fargate tasks and one asymmetric
signing key. Everything else is either request-priced at demonstration volume, inside
a perpetual free tier, or — for the cluster — covered by introductory account credits.
Unit prices are not tracked in this repository and change without reference to it, so
the arithmetic is shown and the prices are marked estimated: an operator planning a
deployment should re-read the current published on-demand prices for the chosen region
and redo the sum rather than trusting this figure.

## Measured storage footprint

**Measured.** The Seed_Generator was run at the design's own default volumes into a
throwaway schema on a local single-node instance, the contamination pass was run over
it, and the footprint was then read back from the cluster's own range catalogue —
`SHOW RANGES FROM DATABASE … WITH DETAILS, INDEXES`, summing each index span's
`live_bytes` per table and per index. The schema was dropped afterwards.

**What the figure corresponds to.** Requested volumes of 4 Clients, 28 Sessions, and
2600 Events, which produced these row counts, read from the tables after both passes:

| Table | Rows |
|---|---|
| `ledger` | 2423 |
| `embedding` | 618 |
| `client_binding` | 628 |
| `working_memory` | 84 |
| `session` | 28 |
| `lineage_edge` | 22 |
| `derived_artifact` | 10 |
| `client` | 5 |

| Table | Live bytes | Share |
|---|---|---|
| `embedding` | 2985562 | 53.7% |
| `ledger` | 2226707 | 40.0% |
| `client_binding` | 296793 | 5.3% |
| everything else, 26 tables | 52127 | 0.9% |
| **total** | **5561189** | **100%** |

That is **5.30 MiB of live data for a 2400-Event corpus**, which is 0.05% of the 10 GiB
ceiling the requirements set. The largest single index is the `embedding` primary
index at 2623577 bytes; the distributed vector index accounts for 201796 bytes, 3.6%
of the total. Secondary indexes are 30.6% of the footprint and primary indexes the
remaining 69.4% — worth knowing before adding another index to `ledger`, which already
answers through eight index spans, one of them its primary.

**Derived from it.** Roughly 2.3 KiB per Event, including that Event's share of every
index, its embedding where it carries one, and its bindings. At that rate the 10 GiB
ceiling corresponds to about 4.6 million Events, so the ceiling is a bound on corpus
size rather than a constraint on the demonstration. An `embedding` row costs about
4.7 KiB, which the 1024-dimension vector's 4096 bytes dominates: the width is the
storage cost of this system, and it is fixed by the schema rather than by a provider.

**Three caveats, each a limitation of the measurement rather than of the system.**
The instance was single-node, so one replica of everything was measured; a managed
cluster keeps three, and how a plan bills replicated bytes is the plan's own matter
and was not measured here. `live_bytes` excludes multi-version garbage still inside
the collection horizon; the same reading reported 6.69 MiB of approximate disk bytes
for the whole database, which includes that garbage and storage-engine overhead. And
the figure scales with corpus size and with text length per Event — the payload cap
and the digest-instead-of-content rule for oversized bodies are what keep the per-Event
figure near constant as agent tools grow chattier.

## Measured request-unit consumption

**Not measured, and this document will not invent it.** Request units are metered by
the managed plan. Every measurement above was taken against a local single-node
instance, which meters none, and no run recorded in this tree has produced a
request-unit figure. The honest statement is that the number is outstanding, so here
is the derivation basis instead, which is what an operator can check the eventual
figure against:

- Request units are charged for read and write batches and for the bytes they move, so
  consumption follows the *statement* count rather than the row count. The bounds are
  therefore structural: the capture side batches Events per hook invocation and
  coalesces its spool, so an agent run costs statements proportional to its batches
  rather than to its Events.
- One recall is one statement. It is index-served with a candidate pool bounded to at
  most 256 rows, so its cost does not grow with the corpus the way an exact scan's
  would. The [measured latency figures](platform.md#measured-recall-latency-on-the-delivered-configuration)
  are the observable side of the same bound.
- Row-level expiry deletes in small batches on the cluster's own schedule rather than
  in one sweep, so the delete cost is spread rather than spiked.
- An Erasure_Run's statements are countable from its phases: one explicit sweep, one
  residue query per query Artifact up to the configured query limit, disposition writes
  batched at the configured batch size, and a fixed set of evidence writes.

The budget the configuration is held to is **50 million request units per month**, and
the figure to compare against it is the one the managed cluster's own metering reports
after the seeded workload has run on it. Until then, treat the budget as a bound the
design was built for and not as a measurement that confirmed it.

## Measured prompt-cache hit ratio

Two numbers matter here and they point in opposite directions, which is why this
section is longer than the arithmetic warrants.

**Measured: the Stable_Prefix the adjudication path actually builds is far below the
cacheable floor.** The prefix is the fixed task instructions plus the section headings
plus the query Artifact's excerpt capped to the configured byte budget. Built through
`Adjudicator.stable_prefix` over the seeded corpus's own text, it measures:

| Query text source | Prefix bytes, smallest | Median | Largest | Cache boundary marked |
|---|---|---|---|---|
| Instructions and headings with no excerpt at all | 759 | — | — | no |
| The 10 seeded `derived_artifact` bodies | 759 | 780 | 815 | 0 of 10 |
| 400 text-carrying `ledger` rows | 734 | 769 | 3739 | 0 of 400 |

The configured `Minimum_Cacheable_Prefix_Length` is **16384 bytes** and the prefix
byte budget is 32768. A prefix of 769 bytes is a twentieth of the floor, so **the
Cache_Boundary is marked on none of these prompts, the measured hit ratio on this
corpus is zero, and every adjudication batch over it is a below-floor batch.**

**That zero is the design working, not the design failing.** Below the floor a cache
write is billed and no later call reads it back, so marking a boundary there costs
more than not caching at all. The prompt *text* is identical either way — the same
two-part structure with the same memoised prefix — so only the marker differs and a
recorded prompt digest stays comparable across the floor. The count of below-floor
batches is emitted per run as `adjudication.prefix_below_floor_batches` precisely so
that a hit ratio of zero is interpretable rather than merely low.

**Derived: the ratio the path produces when the floor is reached.** The prefix is
memoised on the query Artifact identifier for the life of the run and nothing that
varies per candidate is admitted into it, so a group of `n` candidates sharing one
query Artifact presents the same bytes `n` times. The provider charges the first
presentation as a cache write and each later one as a cache read, which is exactly
what the stub text provider's accounting reproduces. So for one group

```text
cache_read / (cache_read + cache_creation) = (n - 1) / n
```

and across a whole run of groups it is `Σ(n_g − 1) / Σn_g`. With the configured
residue top-k of 100 neighbours per query Artifact, a group that fills its band
approaches a hit ratio of 0.99; a group of two candidates gives 0.5; a group of one
gives 0. This is a structural ratio read off the prompt construction and the billing
model, not a vendor claim, and the precondition it rests on — that the prefix is
byte-identical across a group rather than merely equal-looking — is what Property 30
asserts at the provider boundary.

**Why the floor is not lowered by changing models.** The delivered text model is
retained rather than exchanged for one carrying a lower minimum cacheable prefix
length, because its input token price is roughly a third of the alternative's:
exchanging models to unlock caching would raise the bill it was meant to reduce. The
floor stays where the delivered model puts it, and the cost control that does the work
at this corpus size is the threshold band narrowing how many candidates are adjudicated
at all.

## Cost per Erasure_Run

**Derived, with an estimated price.** Three charges attach to a run, and only the first
is meaningfully variable.

**Model spend.** One adjudication call per review-band candidate. From the measurement
above, one call carries about 769 bytes of prefix and about 540 bytes of suffix for a
seeded candidate — roughly 193 and 135 tokens at four bytes per token, so about 330
input tokens per call plus one short JSON answer. The call count is bounded by the
configured residue query limit times the configured top-k, 50 × 100 = 5000 candidates
before banding, of which only those falling strictly between the auto-inclusion
threshold and the review threshold are adjudicated at all. So a worst case where every
candidate lands in the band is about **1.65 million input tokens for one run**, and a
realistic run over the seeded corpus is a small fraction of that. Add one rewrite call
per Blended_Artifact. Converting tokens to currency needs the delivered text model's
published input price, which this repository does not track; at input prices in the low
single dollars per million tokens, the worst case is single-digit dollars per run and
the seeded case is cents.

**Signing and evidence.** One asymmetric signature per certificate and one per
Ledger_Checkpoint, and one object write per run into the Object Lock bucket. Both are
per-request charges at a rate where a run's contribution rounds to nothing; the key's
monthly storage charge is a fixed item in the table below and is not a per-run cost.

**Cluster work.** Not measured, for the reason the request-unit section gives. Its
shape is bounded by the phase structure listed there.

## Per-service monthly consumption and cost

Consumption is estimated at demonstration traffic. Cost is estimated from published
on-demand pricing and the arithmetic is shown wherever it is not a rounding to zero.
No figure in the cost column is a bill this project received.

| Service | Estimated monthly consumption | Estimated monthly cost | What holds it there |
|---|---|---|---|
| CockroachDB Cloud cluster, basic plan | Under the 10 GiB ceiling — 5.30 MiB measured for the seeded corpus — and under the 50 million request-unit budget | **Covered by introductory account credits, not by a perpetual free tier.** Once credits are exhausted, storage and request units are billed | Payload caps on Event text, digest-instead-of-content for oversized bodies, small expiry delete batches, index-served reads with a bounded candidate pool |
| Lambda — Collector | One invocation per hook batch, 512 MB, 30-second ceiling, reserved concurrency 10 | Effectively zero at this volume; Lambda's request and compute free tier is perpetual | Batching in the hook, spool coalescing, no web framework in the ingest path, and the reserved concurrency ceiling bounding a leaked-token flood |
| Lambda — Web_Console | One invocation per page view, 1024 MB, 60-second ceiling, plus one short scheduled invocation per checkpoint interval | Effectively zero at this volume, same free tier | Server-rendered pages, no browser polling beyond the erasure stream, and the checkpoint invocation reading terminal digests through one index-served aggregate |
| ECS Fargate — Policy_Watcher | One task, smallest size, ARM64: 0.25 vCPU and 512 MiB, always on | About US$7. Roughly 0.25 vCPU-hours × 730 hours at about US$0.032 per vCPU-hour plus 0.5 GiB × 730 hours at about US$0.0036 per GiB-hour. **No perpetual free tier** | Smallest task size, one replica, public subnet so no address-translation charge stacks on top |
| ECS Fargate — Molt_MCP_Server | One task, same size, always on | About US$7, same arithmetic. **No perpetual free tier** | As above. The two tasks together are the dominant fixed line item of the whole configuration |
| CloudFront | Demonstration-scale requests and egress, price class limited | Effectively zero; the perpetual free tier covers 1 TB of egress and 10 million requests per month | Default certificate and generated hostname, single origin, no custom domain, no load balancer in front of the function endpoint |
| KMS — one asymmetric signing key | One `ECC_NIST_P256` sign-and-verify key; one signature per Erasure_Run and one per checkpoint interval | About US$1 for key storage plus a per-request charge that rounds to zero at this volume. **Key storage has no perpetual free tier** | One key rather than one per component, and digest signing rather than payload signing, so a request is a fixed small size regardless of certificate length |
| S3 with Object Lock | One certificate object per Erasure_Run plus its versions; kilobytes each | Under US$0.10 | Certificates only and no memory content in the bucket; `GOVERNANCE` retention at a one-day interval, so versions age out and teardown completes |
| Parameter Store, standard tier | 13 parameters | Zero. **The per-secret-charged store was deliberately not used** — that one charges per secret per month with no perpetual free tier | Standard-tier parameters hold every connection string, credential record, and shared secret |
| CloudWatch | 6 log groups at 7-day retention, at most 10 distinct billable metric-and-dimension combinations, 3 alarms | About US$3 for the metric combinations plus about US$0.30 for the alarms plus under US$1 for log ingestion and storage | The configured cardinality bound, which diverts an eleventh combination into a structured log record instead of creating a new billable metric. Log volume is charged by bytes rather than by cardinality |
| Embedding provider | One batched call per 25 embeddable Artifacts | Low single dollars at seeded volume | Batching, and one embedding per Artifact rather than per query |
| Text provider | Adjudication only for candidates inside the review band, one rewrite per Blended_Artifact | See the per-run derivation above: cents for a seeded run, single-digit dollars for a worst-case run | The threshold band, the memoised Stable_Prefix, and the fail-closed path that costs nothing when the provider is unreachable |
| Application Load Balancer, NAT gateway, interface endpoints | **None created** | Zero | Removed by design. An ALB carries an hourly charge with no free-tier allowance and its HTTPS listener needs a certificate that cannot be issued for its own generated hostname; a NAT gateway is unnecessary because both tasks are outbound-connecting in public subnets |
| Bedrock | Zero in the delivered configuration, because on-demand inference quota is zero and non-adjustable on the account | Zero | The provider abstraction, which makes this a configuration state rather than a design change: the path stays documented and deployable |

Adding the fixed items — two Fargate tasks, one key, the metric combinations, the
alarms, and log storage — gives about **US$19 per month**, with the request-priced
services rounding to zero at demonstration volume and the model providers contributing
a few dollars in a month containing several Erasure_Runs. The stated maximum of US$30
is that sum with headroom for a heavier month, and it holds only while the cluster is
on introductory credits.

## The free-tier statement, in as many words

Three of the items above are commonly assumed to be free and are not, so the
assumption is contradicted here rather than left to a footnote.

- **Cluster consumption is covered by introductory account credits, not by a perpetual
  free tier.** Credits run out. When they do, the storage and request-unit figures in
  this document become a bill, which is why both are budgeted rather than merely
  observed.
- **Fargate has no perpetual free tier.** The two always-on tasks are charged from the
  first hour, which is why they are the smallest available size, one replica each, and
  why nothing else in this design is a standing task — the checkpoint signer runs
  inside an existing function on a schedule rather than as a task of its own.
- **Per-secret secret storage has no perpetual free tier**, charging per secret per
  month. That is why every credential in this configuration lives in a standard-tier
  parameter instead, and why the parameter count is 13 rather than growing per
  component.
- **Asymmetric key storage has no perpetual free tier.** One key is stored, shared by
  the certificate path and the checkpoint path, and it is the reason the design signs
  digests rather than payloads.

## How to reproduce the measurements

The storage figure needs a reachable instance and nothing else:

```text
scripts/run_local_db.sh start
export MOLT_DSN="<connection string printed by run_local_db.sh>"
molt migrate
molt seed --seed <integer> --clients 4 --sessions 28 --events 2600 \
  --ground-truth <path outside the tree>
```

Then read the footprint back from the cluster's own catalogue with
`SHOW RANGES FROM DATABASE <database> WITH DETAILS, INDEXES`, summing each row's
`span_stats` live bytes grouped by table and by index name. The prefix figures come
from building `Adjudicator.stable_prefix` over the seeded text and measuring its
encoded length against the configured floor; `MOLT_MINIMUM_CACHEABLE_PREFIX_BYTES` and
`MOLT_ADJUDICATION_PREFIX_BUDGET_BYTES` are the two settings that decide the answer.

## Related documents

- [platform.md](platform.md) — the measured recall latency and the probed capabilities
  the request-unit derivation leans on.
- [providers.md](providers.md) — the fixed 1024-dimension width that dominates the
  measured storage footprint, and how prompt-cache support is read from the model
  rather than assumed.
- [architecture.md](architecture.md) — the topology decisions that removed the load
  balancer, the address translation, and the interface endpoints.
- [setup.md](setup.md) — the deployment order for every stack named in the table.
- [glossary.md](glossary.md) — `Stable_Prefix`, `Cache_Boundary`,
  `Minimum_Cacheable_Prefix_Length`, `Erasure_Run`, `Fargate`, `KMS`,
  `Parameter_Store`, `CloudWatch`.

_Requirements: 33.3, 33.4, 33.5, 33.6, 33.7, 33.8, 33.9, 33.10, 33.11, 33.12, 33.13,
33.14, 30.2, 34.9, 38.5, 38.6, 38.9, 38.10, 38.11, 38.12, 38.13._
