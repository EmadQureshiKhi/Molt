# Traceability

Where a criterion is met, this says where in the tree to check it. Where it is not
met, it says so instead of pointing at something adjacent.

The criteria are the two tables at the end of `.kiro/specs/molt/requirements.md` —
the hard requirements and the judging criteria — carried over with their requirement
references unchanged. Nothing is added to either list, and nothing is dropped from
one because it is inconvenient.

**Two criteria are not met.** No public demonstration web application is deployed,
and no recording exists; the script a recording would follow is not yet written
either. Both are stated as not delivered below rather than described in terms of the
code that would serve them.

Two things bound how the rest should be read. The README's status section is older
than several of the modules it lists as outstanding, so where that section and this
one disagree, the paths in these tables are the check. And the whole-system run is a
scheduled deliverable rather than a gap in the design: the full-flow test of task
32.1 lands in `tests/e2e/`, and until it does, every status below is a status about
parts and their coverage. Where a row reads *pending the full-flow test*, that names
the task that closes it.

## Hard requirements at a glance

| Criterion | Requirements | Primary evidence | Status |
|---|---|---|---|
| Agentic application using CockroachDB as persistent memory | 7–15, 40, 42, 43, 45, 49 | `src/molt/store/`, migrations 001–015, `src/molt/models/tiers.py` | Met |
| Deployed on AWS | 34, 25 | `infra/templates/` (ten stacks), `infra/deploy.sh`, `tests/infra/test_templates.py` | Definitions delivered, deployment not performed |
| At least two CockroachDB tools (five used) | 24, 10, 19, 23.14, 27, 27.10, 39, 23 | Migration 003, `src/molt/policy/watcher.py`, `scripts/provision_*.sh`, `skills/` | Met |
| At least one AWS service | 34, 30, 33 | `infra/templates/kms.yaml`, `storage.yaml`, `parameters.yaml`, `observability.yaml`, `cdn.yaml` | Declared and template-tested, not exercised in a live account |
| Public repo with README, dependencies, example config, seed data, setup instructions, MIT licence | 35, 28, 30.10, 50.8, 51 | `README.md`, `pyproject.toml`, `config.example.toml`, `src/molt/seed/`, [setup.md](setup.md), `LICENSE` | Met, except that publication is out of scope for this tree |
| Functional public demo web app | 25, 34.2, 34.9, 48.10, 49.16, 51.4 | `src/molt/console/`, `web/templates/`, `infra/templates/console.yaml` | **Not met** — nothing is deployed and no URL exists |
| Video under three minutes showing the memory layer | 35.9 | none | **Not met** — no recording, and no script for one |
| Documentation of CockroachDB tools and AWS services used | 34.7, 34.8, 34.12, 37.13, 37.14, 42.14, 51.5 | [architecture.md](architecture.md), [platform.md](platform.md), [mcp.md](mcp.md), [skills.md](skills.md), [providers.md](providers.md), `infra/README.md` | Met for tools and services; the cost record is not yet written |
| Architecture diagram | 35.5 | `assets/molt-architecture.svg`, rendered in [architecture.md](architecture.md) and in the README | Met |

## Judging criteria at a glance

| Criterion | Requirements | Primary evidence | Status |
|---|---|---|---|
| Agentic Memory Design | 42, 7, 9–14, 25, 40, 43, 49 | `src/molt/models/tiers.py`, [memory-tiers.md](memory-tiers.md), `src/molt/store/attribution.py`, `src/molt/store/confidence.py` | Met |
| Technical Implementation | 8, 15–18, 20–23, 36, 37, 43, 44, 45, 46, 47, 48, 50 | `src/molt/store/chain.py`, `src/molt/store/fencing.py`, `src/molt/collector/ingress.py`, `tests/property/` | Met |
| Real-World Impact | 16–18, 21, 22, 24, 25, 39, 40, 43, 48, 51 | `src/molt/erase/`, `src/molt/attest/`, `skills/`, `docs/interface.json` | Met in code; whole-run confirmation pending the full-flow test of task 32.1 |
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
| Schema and its two generations | `src/molt/store/migrations/001_core.sql` through `015_diff_summary.sql`; `tests/integration/test_schema_shape.py`, `test_schema_shape_amended.py` |
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
and 37.6. See [providers.md](providers.md). Nothing here has been exercised against
a live account, because nothing is deployed.

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

Requirements 25, 34.2, 34.9, 48.10, 49.16, and 51.4. **Not met.**

The console exists as code and templates — thirteen route modules under
`src/molt/console/routes/`, sixteen templates under `web/templates/`, unit coverage
across `tests/unit/test_console_*.py`, and a stack definition in
`infra/templates/console.yaml` — but it is not deployed, so there is no public
application and no URL. A reviewer can read the routes and run the unit suite; a
reviewer cannot visit anything.

Two open findings bear on what a deployment would show, and both are recorded in
[reviews.md](reviews.md): the read-only connection the sensitivity grid requires,
and the configured signing key live certificate verification needs.

## Video under three minutes showing the memory layer

Requirement 35.9. **Not met.**

No recording exists. The demonstration script Requirement 35.9 obliges the
documentation to carry — capture on two machines, semantic recall changing an agent
decision, residue detection, an erasure run, and certificate verification — is not
yet written; it is intended as `docs/demo.md`, which does not exist. The sequence a
recording would follow is the sequence the full-flow test of task 32.1 executes, so
that test lands first and the script follows it rather than being invented beside
it.

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

One document in this group is missing: the cost record, which Requirements 33.5,
33.6, and 38.6 oblige. It is intended as `docs/cost.md` and is not yet written,
because it needs measured storage, request-unit, and prompt-cache figures that no
run in this tree has produced.

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

The limit is scheduled rather than structural: no erasure run has yet been performed
against a deployment and no certificate verified from end to end, so for now the
impact claim rests on component coverage. The full-flow test of task 32.1 is what
converts it into a claim about a run.

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
