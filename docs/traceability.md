# Traceability

Where a criterion is met, this says where in the tree to check it. Where it is not
met, it says so instead of pointing at something adjacent.

The criteria are the two tables at the end of `.kiro/specs/molt/requirements.md` —
the hard requirements and the judging criteria — carried over with their requirement
references unchanged. Nothing is added to either list, and nothing is dropped from
one because it is inconvenient.

**One criterion is not met.** No recording exists. Every beat one would show is reachable
against the live deployment, so what is missing is the take rather than the capability, and
it is stated as not delivered below rather than described in terms of the code that would
serve it.

The public demonstration web application is met: the console is deployed at its own
domain, signs an operator in with the credential the README publishes, renders every page
from the live cluster, and serves a signed erasure certificate that verifies against that
cluster.

Two things bound how the rest should be read. Where the README's status section and
this one disagree, the paths in these tables are the check, because a path can be
opened and a status cannot. And the whole-system run now exists: `tests/e2e/test_full_flow.py` drives seed, signed ingest,
confidence-weighted recall, sensitivity analysis, checkpoint, leased erase,
certify, and verify in that order against a live instance, and it passes. So the
statuses below are no longer statuses about parts alone.

What that run establishes, stated narrowly. The eight stages compose: each one's
output is the next one's input, in one history rather than eight setups. The
certificate an authorised erasure produced verifies against the erased cluster —
the outcome an independent verification reports is the verified one, not merely
that a document was produced and parses. Its counts are confirmed through the
derived mechanism, so the agreement does not depend on the garbage-collection
horizon. And four of its fields each agree with a stored row: the ownership
generation with the lease the run held, the named checkpoint with the checkpoint
row the cluster stores, the first-attribution pair with the attribution rows read
before the disposition phase removed the rest, and the working-rows-deleted count
with the number of Working_Memory rows the tenant held before the run and holds
after.

What it does not establish. The run uses stub model and embedding providers and a
stub object store, and it signs with a local key rather than through the key
service, so what is exercised is the assembly and verification path rather than
those services' participation in it. And it runs against a local instance and a
schema of its own, so what it establishes is the path rather than the deployment.

The deployment is separate evidence and it now exists: eleven of twelve stacks run in a
live account against a managed cluster, and the README's [what is
deployed](../README.md#what-is-deployed) section states each fact with the value it was
verified at. Where a row below still reads *not exercised*, it means the deployment does
not reach that particular claim rather than that no deployment exists.

## Hard requirements at a glance

| Criterion | Requirements | Primary evidence | Status |
|---|---|---|---|
| Agentic application using CockroachDB as persistent memory | 7–15, 40, 42, 43, 45, 49 | `src/molt/store/`, migrations 001–022, `src/molt/models/tiers.py` | Met, and exercised against a managed cluster: twenty two migrations applied, four least-privilege roles, a seeded corpus, semantic recall answered from the distributed vector index, and a governed erasure completed with a signed certificate that verifies |
| Deployed on AWS | 34, 25 | `infra/templates/` (twelve stacks), `infra/deploy.sh`, `tests/infra/test_templates.py` | Met — eleven of twelve stacks deployed and exercised in a live account; the content distribution is refused until the provider verifies the account, and the `gateway` stack makes the deployment publicly reachable without it |
| At least two CockroachDB tools (five used) | 24, 10, 19, 23.14, 27, 27.10, 39, 23 | Migration 003, `src/molt/policy/watcher.py`, `scripts/provision_*.sh`, `skills/` | Met |
| At least one AWS service | 34, 30, 33 | `infra/templates/kms.yaml`, `storage.yaml`, `parameters.yaml`, `observability.yaml`, `gateway.yaml` | Met and exercised live: the parameter store holds every credential the deployment reads, the asymmetric key and the Object Lock bucket back the certificate path, functions serve both surfaces, two task services run, and the regional endpoints carry public traffic |
| Public repo with README, dependencies, example config, seed data, setup instructions, MIT licence | 35, 28, 30.10, 50.8, 51 | `README.md`, `pyproject.toml`, `config.example.toml`, `src/molt/seed/`, [setup.md](setup.md), `LICENSE` | Met |
| Functional public demo web app | 25, 34.2, 34.9, 48.10, 49.16, 51.4 | `src/molt/console/`, `web/templates/`, `infra/templates/console.yaml`, `gateway.yaml` | Met — the console is deployed at its own domain, signs an operator in, and renders every page from the live cluster over a seeded corpus. An erasure can be started from it, and the certificate a completed run produced is served and verified through it |
| Video under three minutes showing the memory layer | 35.9 | none | **Not met** — every beat is reachable against the live deployment; no recording exists |
| Documentation of CockroachDB tools and AWS services used | 34.7, 34.8, 34.12, 37.13, 37.14, 42.14, 51.5 | [architecture.md](architecture.md), [platform.md](platform.md), [mcp.md](mcp.md), [skills.md](skills.md), [providers.md](providers.md), [cost.md](cost.md), `infra/README.md` | Met; the cost record labels each figure measured, derived, or estimated, and records request-unit consumption as outstanding |
| Architecture diagram | 35.5 | `assets/molt-architecture.svg`, rendered in [architecture.md](architecture.md) and in the README | Met |

## Judging criteria at a glance

| Criterion | Requirements | Primary evidence | Status |
|---|---|---|---|
| Agentic Memory Design | 42, 7, 9–14, 25, 40, 43, 49 | `src/molt/models/tiers.py`, [memory-tiers.md](memory-tiers.md), `src/molt/store/attribution.py`, `src/molt/store/confidence.py` | Met |
| Technical Implementation | 8, 15–18, 20–23, 36, 37, 43, 44, 45, 46, 47, 48, 50 | `src/molt/store/chain.py`, `src/molt/store/fencing.py`, `src/molt/collector/ingress.py`, `tests/property/` | Met |
| Real-World Impact | 16–18, 21, 22, 24, 25, 39, 40, 43, 48, 51 | `src/molt/erase/`, `src/molt/attest/`, `skills/`, `tests/e2e/test_full_flow.py`, `docs/interface.json` | Met, and confirmed as one run against a live instance; not against a deployment |
| Production Readiness | 27, 30–38, 41, 44–47, 50, 51 | `scripts/`, `infra/`, `.github/workflows/ci.yml`, [typing.md](typing.md), [hygiene.md](hygiene.md), [threat-model.md](threat-model.md) | Met, with the open findings in [reviews.md](reviews.md) |
| Creativity & Originality | 17, 18, 21–23, 29, 37, 39, 42, 44, 45, 48 | `src/molt/erase/residue.py`, `src/molt/erase/sensitivity.py`, `src/molt/attest/checkpoint.py` | Met |

---

## Agentic application using CockroachDB as persistent memory

Requirements 7–15, 40, 42, 43, 45, 49.

Memory is the database rather than a cache in front of one. Six tiers are encoded
once in `src/molt/models/tiers.py` and the tier document is generated from that
encoding by `scripts/generate_tier_doc.py`, so [memory-tiers.md](memory-tiers.md)
cannot drift from the taxonomy it describes.

| Claim | Evidence |
|---|---|
| Schema and its two generations | `src/molt/store/migrations/001_core.sql` through `022_eraser_cascade_deletes.sql` — twenty two files, twenty nine tables; `tests/integration/test_schema_shape.py`, `test_schema_shape_amended.py` |
| Append-only episodic tier with a per-session digest chain | `src/molt/store/chain.py`; `tests/integration/test_ledger_chain_live.py`, `tests/property/test_p06_chain_tamper.py` |
| Semantic recall over a distributed vector index | `src/molt/store/embeddings.py`, `src/molt/recall/`; `tests/integration/test_recall_engine.py`, `tests/perf/test_recall_latency.py` |
| Bitemporal attribution | `src/molt/store/attribution.py`; `tests/integration/test_attribution_supersession.py`, `tests/property/test_p32_attribution_history.py` |
| Confidence-weighted procedural memory | `src/molt/store/confidence.py`, `src/molt/confidence/`; `tests/property/test_p36_procedure_confidence.py` |
| Signed checkpoints over the ledger | `src/molt/attest/checkpoint.py`; `tests/integration/test_checkpoint_verification.py`, `tests/security/test_checkpoint_beyond_admin.py` |
| Disposable working tier with cluster-enforced expiry | `src/molt/store/working.py`; `tests/integration/test_row_level_ttl.py`, `tests/property/test_p37_working_disposability.py` |

## Deployed on AWS

Requirements 34 and 25.

Ten stack definitions, two wrappers, and a template suite that reads them:
`infra/templates/` holds `network.yaml`, `parameters.yaml`, `storage.yaml`,
`kms.yaml`, `collector.yaml`, `console.yaml`, `mcp.yaml`, `watcher.yaml`,
`observability.yaml`, and `cdn.yaml`; `infra/deploy.sh` and `infra/teardown.sh`
drive them; `tests/infra/test_templates.py` asserts their shape and
`scripts/validate_stack_params.py` their parameters. The function entry points the
templates name exist: `src/molt/collector/handler.py` and
`src/molt/console/lambda_adapter.py`.

No stack has been created. There is no account state, no endpoint, and nothing to
visit, so this criterion is delivered as definition and not as deployment.

## At least two CockroachDB tools

Requirements 24, 10, 19, 23.14, 27, 27.10, 39, and 23. Five tools are used.

| Tool | What Molt does with it | Evidence |
|---|---|---|
| Managed MCP server | Auditor access to the cluster through the managed endpoint | `tests/integration/services/test_managed_mcp.py`, [mcp.md](mcp.md) |
| Distributed vector indexing | Recall and residue detection served from a vector index, with an exact-scan fallback on tiers that reject it | `src/molt/store/migrations/003_embedding.sql`, `src/molt/store/capability.py`, `src/molt/store/embeddings.py`; `tests/integration/test_vector_search.py`, `test_fallback_vector_scan.py` |
| ccloud CLI | Cluster and role provisioning, capability interrogation, backup listing, audit-log pull | `scripts/provision_cluster.sh`, `scripts/provision_roles.sh`, `scripts/probe_capabilities.py`, `scripts/pull_audit_log.sh`; `tests/integration/test_capability_probes.py` |
| Agent Skills | Three shipped operator procedures, plus the schema and query review obligation | `skills/verify-certificate/`, `skills/residue-sweep/`, `skills/retention-audit/`; `tests/skills/`; [skills.md](skills.md), [reviews.md](reviews.md) |
| Changefeeds | Policy propagation, with polling retained as the recorded fallback | `src/molt/policy/watcher.py`; `tests/integration/test_changefeed_consumption.py`, `test_polling_fallback.py` |

Each of the five is named with what Molt does with it in
[architecture.md](architecture.md) and [platform.md](platform.md), which is what
Requirement 34.8 asks for beyond mere use.

## At least one AWS service

Requirements 34, 30, and 33.

The delivered configuration uses Lambda, Fargate, KMS, S3 with Object Lock,
Parameter Store, CloudWatch, and CloudFront. Each is declared in a template under
`infra/templates/` and read back by `tests/infra/test_templates.py`; the KMS signing
path has its own unit coverage in `tests/unit/test_key_service.py` and the parameter
path in `tests/unit/test_secrets_accessor.py`.

The requirements table states the Bedrock exception rather than hiding it: on-demand
inference quota is zero and non-adjustable on the delivered account, so model
inference moved off Bedrock while Bedrock remains the documented default under 37.3
and 37.6. See [providers.md](providers.md). Both selected providers are exercised in the
live deployment: each model identifier was verified against its provider before being
written into the deployment, the embedding model answers the 1024 dimensions the schema
and the index are declared at, and the capability probe records both as reachable.

## Public repo with README, dependencies, example config, seed data, setup instructions, MIT licence

Requirements 35, 28, 30.10, 50.8, and 51.

| Artefact | Where |
|---|---|
| README with the memory-tier table and an honest status section | `README.md` |
| Dependency declaration | `pyproject.toml`, `requirements.txt` |
| Example configuration carrying no credential value | `config.example.toml`; `tests/security/test_credential_absence.py` |
| Seed data generator, deterministic | `src/molt/seed/`; `tests/integration/test_seed_determinism.py` |
| Setup instructions | [setup.md](setup.md) |
| MIT licence | `LICENSE` |
| Machine-readable interface specification, glossary, threat model | `docs/interface.json`, [glossary.md](glossary.md), [threat-model.md](threat-model.md); `tests/spec/` |

Publishing is deliberately outside this design: no component, script, or documented
procedure performs a repository publish step, so "public" is a property of where the
tree is hosted rather than something this tree does.

## Functional public demo web app

Requirements 25, 34.2, 34.9, 48.10, 49.16, and 51.4. **Met.**

The console is deployed at its own domain, the address the README names, so a reviewer
visits it rather than reading about it. It signs in with the operator credential the README
publishes, and every page renders from the live cluster over a seeded corpus.

It is a working console rather than a read-only one, and that is a deliberate change from
how it was first deployed. Read-only demonstration mode issued a visitor a session and
refused every route that would change stored memory, which meant the two beats that matter
most — an Erasure_Run and the certificate it produces — were the two a visitor could not
reach, and a system whose whole claim is provable forgetting was asking to be taken on
trust. So the mode is off, anonymous access is refused in its place, and a reviewer can
start a run. The mode itself remains implemented, and the set it refuses is still derived
from the route table's own methods rather than from a list kept beside it.

Behind it: fourteen modules under `src/molt/console/routes/`, of which twelve are the view
modules the package's own import list registers, seventeen templates under
`web/templates/`, unit coverage across `tests/unit/test_console_*.py`, and a stack
definition in `infra/templates/console.yaml`. The two modules that are not view modules
are shared code rather than unregistered routes: `erasure_common.py` for what the erasure
views share, and `tenancy.py` for the Client roster and the page-rendering helper the
fleet, Session, and lineage views read — which is also the one place the demonstration
roster is narrowed, and the one place the active navigation section is supplied.

Deploying it is what established that the view package was imported by nothing, so every
page answered *not implemented* while eighteen written handlers sat unreferenced and the
unit suite passed against handlers the application never attached. That is finding AG in
[reviews.md](reviews.md), and a gate now asserts the join by building the routes through
the application's own call and refusing any route the placeholder would answer.

Two open findings bear on what a deployment would show, and both are recorded in
[reviews.md](reviews.md): the read-only connection the sensitivity grid requires,
and the configured signing key live certificate verification needs.

## Video under three minutes showing the memory layer

Requirement 35.9. **Not met.**

No recording exists. Every beat such a recording would show is reachable — capture on two
machines, semantic recall changing an agent decision, residue detection, an erasure run, and
certificate verification — and the sequence is the one `tests/e2e/test_full_flow.py`
executes, so the order comes from a run rather than from a plan. The console serves each of
them against the live cluster. The criterion stays not met because a reachable path is not a
recording.

## Documentation of CockroachDB tools and AWS services used

Requirements 34.7, 34.8, 34.12, 37.13, 37.14, 42.14, and 51.5.

[architecture.md](architecture.md) names the components and the trust boundaries,
[platform.md](platform.md) records each probed platform fact and what depends on it,
[mcp.md](mcp.md) the tool surface and its transports, [skills.md](skills.md) the
skill format and the review obligation beside it, [providers.md](providers.md) the
model providers and the width gate, [memory-tiers.md](memory-tiers.md) the tier
taxonomy, and `infra/README.md` the stacks. Requirement 34.12's negative fact — the
cluster offers no column-scoped `UPDATE` grant and no updatable view narrowed to
writable columns — is recorded in [protection.md](protection.md) and
[reviews.md](reviews.md), with the guards it forced in migration 014.

The cost record Requirements 33.5, 33.6, and 38.6 oblige is [cost.md](cost.md). It
carries the stated maximum monthly cost and labels every figure **measured**,
**derived**, or **estimated** rather than presenting a judgement as a reading. The
storage footprint and the prompt-cache prefix length are measured against the seeded
corpus on a local single-node instance; request-unit consumption is recorded as **not
measured**, because request units are metered by the managed plan and nothing in this
tree has run against one. The budget the configuration is held to is stated as a
bound the design was built for rather than a measurement that confirmed it.

## Architecture diagram

Requirement 35.5.

`assets/molt-architecture.svg`, rendered inside [architecture.md](architecture.md) beside
the component inventory, the write path, and the erasure path.

---

## Agentic Memory Design

Requirements 42, 7, 9–14, 25, 40, 43, and 49.

The taxonomy is explicit rather than implied: six tiers with a stated mutability
contract each, encoded in `src/molt/models/tiers.py`, enforced by privileges and
constraints rather than by convention, and surfaced to an operator through the
console's tier view (`src/molt/console/routes/tiers.py`,
`web/templates/tiers.html`). What each tier's contract rests on is in
[memory-tiers.md](memory-tiers.md); why deleting evidence is refused is in
[protection.md](protection.md).

## Technical Implementation

Requirements 8, 15–18, 20–23, 36, 37, 43, 44, 45, 46, 47, 48, and 50.

| Mechanism | Evidence |
|---|---|
| Digests computed inside the appending statement | `src/molt/store/chain.py`; `tests/concurrency/test_ledger_appends.py` |
| Fenced erasure leases | `src/molt/store/fencing.py`, `src/molt/erase/lease.py`; `tests/concurrency/test_lease_contention.py`, `tests/property/test_p31_fencing_safety.py` |
| Signed, age-bounded ingress | `src/molt/collector/ingress.py`; `tests/security/test_ingress_replay.py`, `tests/property/test_p34_ingress_signature.py` |
| Referential protection of evidence | `src/molt/store/migrations/013_protection.sql`; `tests/integration/test_referential_restrict.py`, `test_referential_cascade.py` |
| Least-privilege roles and update guards | `007_roles.sql`, `014_grants.sql`; `tests/integration/test_privileges.py`, `test_privileges_amended.py` |
| Typing, linting, and formatting gated in CI | `.github/workflows/ci.yml`, `tests/quality/`; [typing.md](typing.md) |
| Property coverage | `tests/property/` — thirty-eight suites, indexed by the property table in the requirements |

## Real-World Impact

Requirements 16–18, 21, 22, 24, 25, 39, 40, 43, 48, and 51.

The governed erasure path is the substance here: explicit sweep, residue detection,
adjudication, surgical rewriting, dispositions, and a signed certificate
(`src/molt/erase/`, `src/molt/attest/`). What makes it usable by a party that does
not trust the erasing party is that verification needs no privilege on the erasing
side — `src/molt/attest/keys.py` lets an auditor verify from a saved public half —
and that the three operator procedures ship as loadable skills.

This is now a claim about a run rather than about component coverage.
`tests/e2e/test_full_flow.py` seeds a corpus, contaminates it, admits a batch
through the Collector's signed request path, serves a recall page whose ordering a
Learned_Procedure's standing decides, answers a threshold grid over the read-only
role, takes a signed checkpoint, runs a leased erasure, issues a certificate, and
verifies it with nothing but a public key and a read-only connection — and the
verification outcome it asserts is the verified one, with the ownership generation,
the named checkpoint, the first-attribution pair, and the working-rows-deleted
count each checked against the database.

The limit that remains is the deployment. No erasure has been run against a
deployed stack, the providers and the object store in that run are stubs, and the
signature is produced by a key generated in the test process rather than by the key
service. So the composition is demonstrated and the operated-in-an-account claim is
not made.

## Production Readiness

Requirements 27, 30–38, 41, 44–47, 50, and 51.

Provisioning is idempotent and stores no credential in a tracked file
(`scripts/provision_roles.sh`); capabilities are probed rather than inferred from a
version string (`src/molt/store/capability.py`, [platform.md](platform.md)); the
pipeline gates types, lint, format, hygiene, and both test suites
(`.github/workflows/ci.yml`, `tests/ci/test_workflow_definition.py`); and the
security posture is stated with its residues in
[threat-model.md](threat-model.md), including three threats accepted in part.

Open findings that a reviewer should read as part of this criterion rather than
against it are collected in [reviews.md](reviews.md), the largest being the
unauthenticated tool transport, which `tests/security/test_route_authentication.py`
reports as a failing check by design rather than suppressing.

## Creativity & Originality

Requirements 17, 18, 21–23, 29, 37, 39, 42, 44, 45, and 48.

Three mechanisms are the ones worth a reviewer's time. Residue detection treats
semantic descendants of erased material as part of the erasure rather than as a
separate cleanup (`src/molt/erase/residue.py`,
`tests/integration/test_residue_ground_truth.py`). The sensitivity analyser reports
what a threshold pair *would* have reached without replaying any recorded
adjudication, so an operator can calibrate against a live corpus without mutating it
(`src/molt/erase/sensitivity.py`, `tests/property/test_p35_threshold_monotonicity.py`).
And the signed checkpoint extends tamper evidence past a cluster administrator for
one reason only — the signing key lives outside the cluster
(`src/molt/attest/checkpoint.py`, `tests/security/test_checkpoint_beyond_admin.py`).

## Related documents

- [reviews.md](reviews.md) — the schema and query reviews behind several rows above,
  and the open findings.
- [threat-model.md](threat-model.md) — the seven threats, their statuses, and the
  three accepted in part.
- [platform.md](platform.md) — the probed platform facts each degradation path turns
  on, and the measured recall latency.
- [glossary.md](glossary.md) — every term used above, defined once.
- [architecture.md](architecture.md) — the component inventory and the diagram.
