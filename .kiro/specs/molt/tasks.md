# Implementation Plan: Molt

## Overview

Molt is built from nothing in this workspace. Task 1 creates the repository skeleton, the dependency manifest, the static-analysis gates, the metadata-hygiene gate, and the continuous integration workflow; every task after it builds only on completed work. The order follows the dependency direction of the design: pure models and logic first, then the model provider abstraction, then the CockroachDB Memory_Store across both migration generations, then capture, then signed ingest and recall, then the erasure pipeline with its lease ownership and its evidence, then the governance surfaces including the Molt_MCP_Server and the shipped Agent_Skills, then provisioning and infrastructure, then the Interface_Specification and the documentation set.

Implementation language is Python, with the 3.11 floor the runtime declaration obliges and the interpreter invoked explicitly as `python3.12` in every script and workflow step. SQL migrations, CloudFormation YAML, shell scripts, Jinja2 templates, declarative Agent_Skill definitions, one OpenAPI document, one workflow definition, and a small progressive-enhancement stylesheet and script complete the surface.

## Plan-wide constraints

These govern every task below and are not repeated per task.

- **Metadata hygiene.** Every comment, docstring, and documentation file written by any task must carry no personal name, author name, email address, calendar date, clock time, timestamp literal, version-history entry, or third-party project name. Comments describe behavior and intent only. The only exceptions are the platform and vendor names listed in `scripts/hygiene_allowlist.txt` and the copyright line the MIT licence text requires in `LICENSE`. Every task must leave `scripts/hygiene.py` exiting 0, and that scan covers tracked files only. _Requirements: 29.1, 29.4, 29.5, 29.8_
- **No repository publishing.** No task creates a commit, pushes to a remote, opens a change proposal, publishes the repository, or performs any repository-publishing step. Adding the workflow definition in task 1.6 is a source change and is no publish step. _Requirements: Out of Scope 12, 41.4_
- **No credentials in source.** No task writes a connection string, bearer token, private key, password, ingress signing secret, or model provider credential value into any tracked file. Secrets resolve from files under `.secrets/` locally and from Parameter_Store in the deployed path, and from nowhere else. _Requirements: 30.1, 30.2, 30.12_
- **Reference material.** The `reference/` directory is ignored, is read by no script the repository ships including the hygiene scan, and is copied from by no task. Nothing in it reaches any tracked file. _Requirements: 29.1, 29.2, 29.3_
- **Property tests.** Each of the forty design properties is implemented as exactly one Hypothesis test at `tests/property/test_p<NN>_<slug>.py`, running at least 100 examples via `@settings(max_examples=100)`, and carrying the tag comment form the design specifies:
  ```python
  # Feature: molt, Property N: <property text>
  ```
- **No model provider call inside a property loop.** Every property that needs a vector uses deterministic stub vectors and every property that needs generated text uses a stub provider; real providers are exercised only by the service integration suite.
- **No provider SDK outside `src/molt/providers/`.** Provider selection is configuration-driven through the Provider_Selector, and no task may depend on any one provider being invocable.
- **Static analysis precedes tests.** The strict type check, the type-ignore allowlist check, the linter check, and the formatter check are workflow steps 1 through 4 and run before the hygiene check and before any test suite. Every module a task writes carries annotations on every parameter, every return, and every module-level attribute. _Requirements: 50.1, 50.2, 50.6_
- **Tasks marked `*`** are test tasks and may be skipped for a faster path; the code they verify is never optional.

## Tasks

- [x] 1. Bootstrap the repository, the dependency manifest, the static-analysis gates, the hygiene gate, and the CI workflow
  - [x] 1.1 Create the project scaffold and dependency manifest
    - Write `pyproject.toml` declaring the Python 3.11 floor, exact pins for `psycopg`, `boto3`, `starlette`, `uvicorn`, `jinja2`, `cryptography`, `hypothesis`, `pytest`, `mypy`, and `ruff`, and the `molt` and `molt-hook` console entry points
    - Mirror the exact pins into `requirements.txt`
    - Create the directory skeleton from the design Repository Layout: `src/molt/{config,models,capture/adapters,redact,store/migrations,collector,providers,embed,recall,erase,confidence,attest,policy,retention,backup,mcpserver,console,seed,telemetry,cli}/`, `web/{templates,static}/`, `skills/{verify-certificate,residue-sweep,retention-audit}/`, `.github/workflows/`, `infra/{templates,params}/`, `scripts/`, `tests/{unit,property,concurrency,integration,integration/services,e2e,security,perf,skills,mcp,infra,ci,quality,spec,fixtures}/`, `docs/hooks/`, each with the package `__init__.py` files the import path needs
    - Write `.gitignore` listing `reference/`, `.secrets/`, build artefacts, virtual environments, spool files, and `seed/ground_truth.json`
    - Write `LICENSE` with the MIT licence text and its required copyright line
    - Write `config.example.toml` covering every configuration key in the design Configuration Surface table with a non-secret placeholder and no secret defaults, including the provider selection keys, the provider credential parameter and file keys, the prompt-cache key, the Collector body-bound and reserved-concurrency keys, the ingress signing secret reference and the maximum request age key, the lease interval key, the checkpoint interval key, the working-tier expiry interval key, the procedure confidence initial, increment, decrement, and recall floor keys, the Threshold_Grid keys, the metric-cardinality key, and the MCP server keys
    - _Requirements: 29.2, 30.10, 35.1, 35.4, 35.6, 35.7, 35.8, 50.8_

  - [x] 1.2 Configure the test runner and the local database fixture
    - Add the `pytest` configuration to `pyproject.toml`: test paths, the markers `integration`, `services`, `e2e`, `perf`, `concurrency`, `skills`, `mcp`, `quality`, and `spec`, and strict marker enforcement
    - Write `scripts/run_local_db.sh` invoking `python3.12` where a Python step is needed and starting and stopping a single-node local CockroachDB instance for tests, idempotently, printing a DSN on standard output
    - Write `tests/conftest.py` with a session-scoped instance fixture, a module-scoped fresh-schema fixture, stub embedding and text provider fixtures, an injected time-source fixture the lease and ingress properties drive instead of sleeping, and skip behaviour with a clear message when no instance is reachable or no cloud and provider credentials are present, so the unit, quality, spec, and pure-property suites run with no credential of any kind
    - _Requirements: 36.2, 36.15, 41.3, 50.7_

  - [x] 1.3 Configure the strict type check, the linter, the formatter, and the type-ignore allowlist check
    - Add the strict type-check configuration to `pyproject.toml` covering `src/molt/`, `tests/`, and `scripts/`, rejecting untyped definitions, implicit `Any` returns, and unfollowed imports, and add the linter and formatter configuration covering `src/molt/`, `tests/`, `scripts/`, and `infra/`
    - Write `scripts/check_type_ignores.py` scanning tracked source for type-check ignore directives, comparing the found set against `scripts/type_ignore_allowlist.txt`, exiting non-zero when a directive appears in a file the allowlist names no entry for and also when an allowlist entry names a directive that no longer exists, and printing `path:line:directive` lines
    - Write `scripts/type_ignore_allowlist.txt` with the file path, the exact directive, and the reason per entry, and start it empty with the format documented in the file
    - _Requirements: 50.1, 50.2, 50.3, 50.4, 50.5, 50.7, 50.8_

  - [x] 1.4 Implement the metadata-hygiene check
    - Write `scripts/hygiene.py` scanning only tracked paths it builds by walking the tree and applying the ignore rules, over the extension set in the design, with the eight pattern classes: email address, calendar date, clock time, timestamp literal, version-history entry, copyright and authorship attribution, personal-name denylist, reference-project denylist
    - Write `scripts/hygiene_denylist.txt` and `scripts/hygiene_allowlist.txt`, with the denylist excluded from its own scan and that exclusion stated in the file, and the allowlist carrying the database product and its tooling, the cloud provider and each named service, and the five Agent_CLI product names
    - Implement exit behaviour: 0 with per-class scanned-file counts, 1 with `path:line:class:matched-span` lines truncated to 40 characters and a total, 2 on a malformed list file, plus a `--json` flag; scan `LICENSE` for denylist classes only
    - _Requirements: 29.2, 29.3, 29.4, 29.8, 35.1_

  - [x]* 1.5 Write the hygiene-check self-test
    - Create `tests/security/test_hygiene_check.py` asserting exit code 1 with the correct pattern class named when each prohibited pattern is introduced into a temporary tree, exit code 0 for allowlisted vendor names, exit code 2 for a malformed list file, and that no path under the ignored reference directory is ever opened
    - _Requirements: 29.3, 29.8, 36.16_

  - [x] 1.6 Write the continuous integration workflow definition
    - Write `.github/workflows/ci.yml` running six steps in this fixed order: the strict type check over `src/molt/`, `tests/`, and `scripts/`; `scripts/check_type_ignores.py`; the linter check over the four paths; the formatter check over the same four paths; `scripts/hygiene.py`; then the unit suite followed by the property suite, with the marked database-backed, service, concurrency, end-to-end, and performance suites deselected
    - Invoke every Python step as `python3.12`, fail the workflow on any failing step, permit no step to continue on error, reference no credential name or secret in any step, and perform no commit, push, or publish action
    - _Requirements: 41.1, 41.2, 41.3, 41.4, 50.6_

  - [x]* 1.7 Write the CI workflow definition test
    - Create `tests/ci/test_workflow_definition.py` parsing `.github/workflows/ci.yml` and asserting it invokes the strict type check, the type-ignore allowlist check, the linter check, the formatter check, the hygiene check, the unit suite, and the property suite in that order, that the four static checks precede both suites, that no step is permitted to continue on error, and that no step references a cloud provider credential name or a cluster credential name
    - Make every assertion against the parsed structure, walking the step sequence and the per-step fields, rather than matching the definition text, because a structural assertion over a definition outlives a text match
    - Parse with the YAML parser declared in the dependency manifest of task 1.1, pinned to an exact version like every other pin there
    - _Requirements: 41.1, 41.2, 41.3, 41.5, 41.6, 50.6_

  - [x]* 1.8 Write the quality gate suite
    - Create `tests/quality/test_type_check.py` asserting the strict type check reports no error over `src/molt/`, `tests/`, and `scripts/`; `tests/quality/test_lint_format.py` asserting the linter and the formatter report no violation over those three paths plus `infra/`; and `tests/quality/test_type_ignore_allowlist.py` asserting the allowlist check exits non-zero for an unlisted directive and for a stale allowlist entry and exits 0 for a listed directive
    - Mark the suite so it needs no cloud provider credential and no cluster credential
    - _Requirements: 50.1, 50.2, 50.4, 50.5, 50.7, 50.9_

- [x] 2. Implement models, canonical serialisation, redaction, configuration, and the telemetry surface
  - [x] 2.1 Implement the Event, Session, and artifact models
    - Write `src/molt/models/event.py`, `session.py`, `artifact.py`, and `binding.py` with the frozen slotted dataclasses of the design, the Event categories including `recall`, `policy_halt`, and `attribution_superseded`, timezone-aware microsecond timestamps, the Attribution_Version fields carrying validity start, validity end, and superseding reference, the Procedure_Confidence field on the Learned_Procedure kind, and the canonical Event serialiser and deserialiser
    - Decode non-UTF-8 payload bytes with replacement at the capture boundary so an Event never holds undecodable content
    - _Requirements: 7.5, 7.7, 7.8, 9.1, 9.2, 9.5, 9.6, 43.1, 49.1_

  - [x]* 2.2 Write property test for Event round trip
    - **Property 10: Event round trip**
    - **Validates: Requirements 7.5, 7.7, 5.6**
    - Create `tests/property/test_p10_event_round_trip.py` with the `events()` generator over all categories, arbitrary JSON payloads, non-UTC offsets, and optional fields present and absent

  - [x] 2.3 Implement the single canonicaliser
    - Write `src/molt/attest/canonical.py` implementing the nine canonical serialisation rules: UTF-8 without a byte order mark, keys sorted by code point at every level, no insignificant whitespace, every number as a decimal string with six fractional digits for thresholds, distances, and confidence values, explicit `null` for absent optional values, RFC 3339 timestamps with numeric offset and microsecond precision, lowercase hyphenated UUIDs, declared array sort keys, and abort on a non-finite value
    - This is the only canonicaliser in the codebase; certificate signing, checkpoint root digests, and every verification path all call it
    - _Requirements: 21.3, 21.11, 45.3_

  - [x] 2.4 Implement the Redactor
    - Write `src/molt/redact/patterns.py` and `src/molt/redact/__init__.py` with one pre-compiled alternation over the pattern classes: AWS access key identifiers, AWS secret access keys in assignment context, PEM private key blocks, bearer tokens, credentialed connection strings, and values whose sibling key matches the sensitive-name set
    - Replace matches with the fixed `[MOLT_REDACTED]` token, preserve key sets, sequence lengths, and non-string scalar types exactly, stop recursion at depth 32 preserving container type, return the modified flag that sets the Event `redacted` field, and pass payloads through with a warning log record when the redaction-disabled flag is set
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_

  - [x]* 2.5 Write property test for redaction idempotence
    - **Property 8: Redaction idempotence**
    - **Validates: Requirements 4.1, 4.4**
    - Create `tests/property/test_p08_redaction_idempotence.py` with the `payloads()` generator: recursive JSON to depth 6, neutral and sensitive-shaped keys, secret-shaped values per pattern class

  - [x]* 2.6 Write property test for redaction structure preservation
    - **Property 9: Redaction structure preservation**
    - **Validates: Requirements 4.3, 4.4, 4.5**
    - Create `tests/property/test_p09_redaction_structure.py` reusing `payloads()` plus the depth-40 variant that exercises the depth cap, and asserting the `redacted` flag is true exactly when output differs from input

  - [x]* 2.7 Write the redaction performance test
    - Create `tests/perf/test_redaction_latency.py` asserting redaction of a 256 KiB payload completes within 50 milliseconds
    - _Requirements: 4.7_

  - [x] 2.8 Implement configuration resolution, the secret accessors, and the local telemetry surface
    - Write `src/molt/config/resolve.py` implementing environment variable over configuration file over default for every key in the design Configuration Surface table, with a named error printing the missing key name and no secret defaults
    - Write `src/molt/config/secrets.py` with one cached Parameter_Store accessor over a per-process `boto3` client reading standard-tier parameters, a local file accessor reading operator-provided credential files under a configured directory with permissions checked, a single retry on a transient error, the local DSN bypass refused when the production environment flag is set, and errors that print the parameter or file name and never the value
    - Write `src/molt/telemetry/__init__.py` with the `metric`, `log`, and `correlation` surfaces backed by buffered in-process counters and single-line JSON records on standard error, the content-key field filter, and the billable cardinality accounting: track the set of distinct metric-and-dimension combinations already emitted, stop at the configured maximum of 10, divert further combinations to structured log records carrying the same name, value, and dimensions, and increment the undimensioned `telemetry.cardinality_overflow` counter; CloudWatch delivery is wired in task 28
    - _Requirements: 26.5, 26.6, 30.1, 30.2, 30.3, 30.12, 31.2, 31.4, 33.13, 33.14, 37.11_

  - [x]* 2.9 Write unit tests for configuration, the secret accessors, and cardinality accounting
    - Create `tests/unit/test_config_resolution.py`, `tests/unit/test_secrets_accessor.py`, and `tests/unit/test_telemetry_cardinality.py` covering precedence order, the missing-value error naming the key, secret suppression in error text, the production bypass refusal, the operator credential file path accessor, the field filter dropping every content key, and the overflow counter staying undimensioned
    - _Requirements: 26.5, 26.6, 30.2, 30.12, 31.4, 33.13, 33.14_

  - [x]* 2.10 Write property test for the metric cardinality bound
    - **Property 40: Metric cardinality bound**
    - **Validates: Requirements 33.13, 33.14**
    - Create `tests/property/test_p40_metric_cardinality.py` with the `metric_streams()` generator of 1 to 200 emissions in randomised arrival order with a dimension-value pool larger than the maximum and the maximum drawn from 1 to 20, asserting the sink never exceeds the maximum and every suppressed emission appears as a log record with the same name, value, and dimensions

  - [x] 2.11 Implement the Memory_Tier mapping module
    - Write `src/molt/models/tiers.py` as one module-level immutable mapping carrying, per Memory_Tier, the tier name, the tables that tier holds, the mutability of that tier, and the CockroachDB capability that tier relies on, covering all six tiers
    - This is the single place the taxonomy is encoded: the console Memory_Tier view reads this mapping and the generator that produces the memory-tier documentation reads the same mapping, so the design table, the rendered view, and the documentation cannot drift apart
    - _Requirements: 42.1, 42.2, 42.3, 42.4, 42.5, 42.6, 42.7, 42.14_

- [x] 3. Checkpoint - pure logic verified
  - Ensure all tests pass, the four static checks report clean, and the hygiene check exits 0, ask the user if questions arise.

- [x] 4. Implement the model provider abstraction
  - [x] 4.1 Implement the provider protocols, the prompt and result models, and the registry
    - Write `src/molt/providers/__init__.py` with the `EmbeddingProvider` protocol carrying `name`, `model_id`, `dimensions`, `embed`, and `probe`, the `TextProvider` protocol carrying `name`, `model_id`, `supports_prompt_cache`, `generate`, and `probe`, the frozen slotted `Prompt` dataclass with `stable_prefix`, `variable_suffix`, and `cache_boundary`, the frozen slotted `TextResult` dataclass with `text`, `model_id`, `input_tokens`, `output_tokens`, `cache_creation_tokens`, and `cache_read_tokens`, and the `ProviderProbe` result type
    - Write `src/molt/providers/registry.py` as the name-to-implementation mapping for both roles, so selection is by name and switching provider requires a change to no source file
    - Write `src/molt/errors.py` with the full exception taxonomy of the design, including `ProviderError`, `ModelUnavailable`, `ProviderWidthMismatch`, `StaleFencingGeneration`, `LeaseNotHeld`, `LeaseRefused`, `AuditRecordProtected`, `AttributionImmutable`, `CheckpointDisagreement`, and `IngressRejected`
    - _Requirements: 37.1, 37.2, 37.5, 38.1_

  - [x] 4.2 Implement the Provider_Selector, the startup width gate, and the credential loader
    - Write `src/molt/providers/selector.py` with `select_embedding_provider`, `select_text_provider`, `validate_at_startup`, and `load_credential`, resolving the embedding and text provider names from the configuration surface against the registry and raising a configuration error naming the value and the registry keys on an unknown name
    - Implement `load_credential` reading only Parameter_Store or an operator-provided file path, wrapped so the loaded value renders as the fixed placeholder in every log record, exception message, error detail, and output stream
    - Implement the width gate: probe the embedding provider, compare the reported width against the schema constant 1024, and on any other width print the reported width and the required width and exit non-zero before any Embedding is written
    - Probe the text provider for reachability, record the prompt-cache capability in the capability record, and record the selected provider name and model identifier for each of the Embedder, the Adjudicator, and the Redaction_Rewriter
    - _Requirements: 37.5, 37.8, 37.9, 37.11, 37.12, 37.13, 38.3, 38.4_

  - [x] 4.3 Implement the Bedrock provider as the documented default for both roles
    - Write `src/molt/providers/bedrock.py` implementing `EmbeddingProvider` and `TextProvider` through `boto3` in the deployment region, setting `supports_prompt_cache` from the model's own reported capability rather than assuming it, and mapping every failure cause to `ModelUnavailable`
    - Import no provider SDK anywhere outside `src/molt/providers/`, and let an unreachable Bedrock model surface as `ModelUnavailable` rather than as a startup failure, so no other task depends on Bedrock being invocable
    - _Requirements: 34.3, 34.10, 37.3, 37.6_

  - [x] 4.4 Implement the delivered external embedding and text providers
    - Write `src/molt/providers/external_embedding.py` implementing `EmbeddingProvider` against the code-specialised retrieval model returning 1024 dimensions, reporting its declared width from `probe`
    - Write `src/molt/providers/external_text.py` implementing `TextProvider` with `supports_prompt_cache` true, sending the `Prompt` as the Stable_Prefix followed by the variable suffix with the Cache_Boundary marked, and populating `cache_creation_tokens` and `cache_read_tokens` on `TextResult` from the provider's usage response
    - Collapse every failure cause in both implementations to `ModelUnavailable`, and write no credential value to any stream
    - _Requirements: 37.4, 37.7, 37.10, 37.12, 38.3, 38.5_

  - [x]* 4.5 Write unit tests for registry resolution, the width gate, and credential rendering
    - Create `tests/unit/test_provider_registry.py` asserting each configured name resolves to its implementation and an unknown name raises the named configuration error
    - Create `tests/unit/test_provider_width_gate.py` driving a stub reporting a width other than 1024 and asserting the non-zero exit with both widths printed and no Embedding written
    - Create `tests/unit/test_provider_credential_render.py` asserting a loaded credential renders as the fixed placeholder in log records, exception messages, and output streams
    - _Requirements: 37.5, 37.9, 37.12_

- [x] 5. Write the first schema migration generation, 001 through 007
  - [x] 5.1 Implement the migration runner and migration 001
    - Write `src/molt/store/migrate.py` applying numbered files in ascending order inside a transaction each, recording version, name, and file digest in `schema_migration`, idempotent on a second run, with a savepoint wrapper for statements permitted to fail, and refusing to run when a recorded digest no longer matches its file so an edited applied migration is reported rather than silently re-applied
    - Write `src/molt/store/migrations/001_core.sql` with `schema_migration`, `client` including the reserved `unassigned` row, `session` with its depth and outcome constraints and indexes, `ledger` with the non-null Client column, digest constraints, sequence and predecessor uniqueness constraints, the category constraint, the five indexes, and the deferred `session.spawning_event_id` foreign key
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.7, 7.8, 7.9, 7.10, 8.1, 9.1, 9.2, 9.3, 9.5, 9.6, 9.7, 27.2, 27.9, 29.6_

  - [x] 5.2 Write migration 002 for derived artifacts, lineage, and bindings
    - Write `src/molt/store/migrations/002_derived.sql` with `derived_artifact`, `lineage_edge` including the self-edge and uniqueness constraints and both traversal indexes, `client_binding` with the confidence range check and the total `binding_unique_pair` constraint that migration 008 later replaces, and the `artifact_ref` view over the three Artifact kinds
    - _Requirements: 11.1, 11.8, 12.5, 12.7_

  - [x] 5.3 Write migration 003 for embeddings, the required vector index, and the capability record
    - Write `src/molt/store/migrations/003_embedding.sql` with `embedding` carrying the fixed `VECTOR(1024)` column, the `provider` column alongside `model_id`, the dimension check, the uniqueness constraint spanning artifact, kind, provider, and model, the covering `embedding_by_client` and `embedding_by_artifact` indexes, and the `capability` table
    - Create the distributed vector index as `CREATE VECTOR INDEX IF NOT EXISTS ... (v vector_l2_ops)`, read the index definition back, and record `vector_index` as available with the reported operator class in `capability`; a rejection on another tier records the absence from the savepointed outcome and continues, because the fixed column width and every query's SQL text are unchanged either way
    - _Requirements: 10.2, 10.3, 10.6, 10.11, 10.12, 37.15_

  - [x] 5.4 Write migration 004 for erasure evidence
    - Write `src/molt/store/migrations/004_erasure.sql` with `erasure_request`, `erasure_run` including the ordered-threshold check and the `run_active_by_client` partial index, `erasure_candidate` with its selection-reason check, `residue_candidate`, `disposition` with the binding slug arrays and the Artifact identifier held as a plain column, `run_session`, `backup_record` carrying the backup path value and the taken and referenced flags, `erasure_certificate`, and `audit_log_snapshot`
    - _Requirements: 16.1, 16.7, 17.7, 18.5, 18.8, 19.2, 19.7, 21.14, 46.4_

  - [x] 5.5 Write migration 005 for policy state
    - Write `src/molt/store/migrations/005_policy.sql` with `policy_rule` including the five match kinds, the four actions, and the shape-validity check, `policy_match` and `approval_queue` with their deduplication uniqueness constraints and pending index, and `watcher_watermark`
    - _Requirements: 23.5, 23.8, 23.10_

  - [x] 5.6 Write migration 006 for Row-Level TTL on the content tables
    - Write `src/molt/store/migrations/006_retention.sql` setting the expiration expression, daily job cron, and small delete batch size on `ledger`, `derived_artifact`, and `embedding`
    - _Requirements: 14.1, 14.2, 14.6_

  - [x] 5.7 Write migration 007 for roles and privileges
    - Write `src/molt/store/migrations/007_roles.sql` creating `molt_writer`, `molt_eraser`, `molt_reader`, and `molt_watcher`, granting exactly the privilege sets in the design, column-scoping the writer `UPDATE` on `session` counters and terminal fields and on `client_binding` to the validity end and superseding reference columns only, granting the watcher `UPDATE` on the Session halt fields and `INSERT` on `policy_match` and `approval_queue` with `SELECT` only on `ledger`, and revoking `UPDATE` on `ledger` from every role
    - The reader role is what the Certificate_Verifier, the Sensitivity_Analyzer, the Policy_Watcher read path, the Molt_MCP_Server, and the Auditor views connect with, so each no-mutation guarantee is structural
    - _Requirements: 7.6, 27.3, 27.4, 27.5, 30.4, 40.5, 43.2, 43.9, 48.5_

  - [x]* 5.8 Write schema and privilege introspection tests for the first generation
    - Create `tests/integration/test_schema_shape.py` asserting the UUID keys, the Client column present from migration 001, the `TIMESTAMPTZ` and `JSONB` and `VECTOR` column types, the `provider` column and the provider-spanning uniqueness constraint on `embedding`, the vector index present with the reported operator class recorded in `capability`, every named index, and the constraint set
    - Create `tests/integration/test_privileges.py` asserting no role holds `UPDATE` on `ledger`, the eraser holds `DELETE` and not `UPDATE`, the reader holds `SELECT` only, the writer's `UPDATE` on `client_binding` is column-scoped, and migration re-application changes no state
    - _Requirements: 7.6, 10.3, 10.12, 27.2, 27.9, 29.6, 30.4, 36.2, 37.15, 43.9_

- [x] 6. Write the second schema migration generation, 008 through 022
  - [x] 6.1 Write migration 008 for bitemporal attribution
    - Write `src/molt/store/migrations/008_attribution.sql` adding the validity start, validity end, and superseding reference columns to `client_binding` with `IF NOT EXISTS`, dropping `binding_unique_pair`, and creating the partial unique index `binding_current_unique` over unsuperseded versions only so a history accumulates while exactly one version stays current per Artifact and Client pair
    - Add the total closure check pairing validity end with the superseding reference, the ordered-interval check, and the `binding_as_of` index on artifact and descending validity bounds storing client, method, confidence, and the superseding reference so the as-of query is an index range with no row fetch
    - Extend the Ledger category constraint with `attribution_superseded`, replacing the constraint rather than editing migration 001
    - _Requirements: 12.7, 42.3, 43.1, 43.2, 43.8, 43.10_

  - [x] 6.2 Write migration 009 for erasure leases and the fencing generation
    - Write `src/molt/store/migrations/009_lease.sql` creating `erasure_lease` with client, owner, generation, idempotency key, acquisition, expiry, renewal, and supersession columns, the positive-generation, expiry-ordering, and closure-consistency checks, the `lease_history_by_client` index making the per-Client generation maximum a single seek, the `lease_current_unique` partial index admitting at most one current lease per Client, and the `lease_idempotency_unique` index
    - Add the fencing generation, lease reference, idempotency key, finalisation marker, finalisation result, and working-rows-deleted columns to `erasure_run`, the run idempotency partial unique index, and the fencing generation columns on `disposition` and `erasure_certificate`
    - _Requirements: 42.6, 44.1, 44.2, 44.3, 44.9, 44.10, 44.11, 42.13_

  - [x] 6.3 Write migration 010 for signed ledger checkpoints
    - Write `src/molt/store/migrations/010_checkpoint.sql` creating `ledger_checkpoint` with the window bounds, covered Session count, root digest, signature, KMS key identifier, and signing algorithm, the window-ordering, digest-length, and non-negative-count checks, and the `checkpoint_by_window_end` index serving the certificate's most-recent-before lookup
    - Create `checkpoint_session` keyed by checkpoint and Session with the terminal chain digest and terminal sequence recorded at checkpoint time, its Session identifier deliberately carrying no foreign key so a checkpoint outlives an erased Session
    - Configure no Row-Level TTL on either table, explicitly, because a checkpoint's value is that it stays checkable after the rows it commits to have gone
    - _Requirements: 42.5, 45.5, 45.7, 45.10, 45.11_

  - [x] 6.4 Write migration 011 for the working tier
    - Write `src/molt/store/migrations/011_working.sql` creating `working_memory` keyed by Session identifier and scratch key, carrying a `JSONB` value column, a Client identifier, an update timestamp, and an expiry timestamp defaulting to the configured 3600 second interval, with the `working_by_client` index serving the single per-Client purge statement
    - Set the row-level TTL expiration expression, an hourly job cron rather than a daily one so the tier's disposability is real, and the delete batch size
    - Extend the `artifact_ref` view with nothing, and state in the migration that the omission is what structurally prevents a working row from becoming a lineage parent, an erasure candidate, or a binding subject
    - _Requirements: 14.1, 42.7, 42.8, 42.9, 42.12, 42.15_

  - [x] 6.5 Write migration 012 for confidence-weighted procedural memory
    - Write `src/molt/store/migrations/012_confidence.sql` adding the Procedure_Confidence column to `derived_artifact`, the unit-interval range check, the equivalence check making confidence present exactly for the Learned_Procedure kind, and the partial `derived_procedure_confidence` index serving both the recall tie-break and the floor predicate
    - Create `procedure_retrieval` with its per-procedure and per-Session indexes, `procedure_outcome` with the known-outcome check and the per-Session uniqueness constraint so one Session moves confidence at most once per procedure, and `procedure_confidence_change` with the range check, the actually-changed check, and the ascending `change_by_procedure` index serving the ordered history query
    - _Requirements: 42.4, 49.1, 49.2, 49.8, 49.9, 49.12, 49.15_

  - [x] 6.6 Write migration 013 for the referential protection of audit records
    - Write `src/molt/store/migrations/013_protection.sql` re-creating every foreign key referencing an Erasure_Request, an Erasure_Run, an Erasure_Lease, or a Ledger_Checkpoint with `ON DELETE RESTRICT`, covering `erasure_run`, `erasure_candidate`, `residue_candidate`, `disposition`, `run_session`, `backup_record`, `erasure_certificate`, `audit_log_snapshot`, and `checkpoint_session`, each guarded so re-application is a clean no-op
    - Add no foreign key to the Disposition's Artifact identifier, and state in the migration that a Disposition must outlive the Artifact it describes
    - Drop the two self-referencing foreign keys in the same migration: the one on the superseding Attribution_Version reference of `client_binding` and the one on the superseding Erasure_Lease reference of `erasure_lease`, each guarded so re-application is a clean no-op
    - State the reason in the migration: the cluster refuses a single statement performing two mutations of one table through a common table expression, so a supersession is two ordered statements in one SERIALIZABLE transaction, and that shape briefly requires a row to reference a successor that is not yet inserted; the integrity of each superseding reference comes from the transaction rather than from the constraint, which is the reasoning already applied to `disposition.artifact_id` and `checkpoint_session.session_id`
    - Landing both drops here rather than reworking 008 and 009 means no already-applied migration is edited, so no recorded migration digest breaks
    - Re-create the recomputable references with `ON DELETE CASCADE`: the Embedding's client reference, the Lineage_Edge's child reference, and the Working_Memory Session reference
    - _Requirements: 46.1, 46.3, 46.4, 46.9, 42.5, 43.12, 43.13, 44.17, 44.18_

  - [x] 6.7 Write migration 014 for the role grants of the new tables
    - Write `src/molt/store/migrations/014_grants.sql` carrying every grant for the tables added in 008 through 013, because an applied migration is never edited and folding these into 007 would break its recorded digest
    - Grant the writer `SELECT` and `INSERT` on `working_memory` with `UPDATE` column-scoped to value, update timestamp, and expiry, `SELECT` and `INSERT` on the three procedure tables, and `UPDATE` on the Procedure_Confidence column of `derived_artifact` only
    - Grant the eraser `SELECT` and `INSERT` on `erasure_lease` with `UPDATE` column-scoped to expiry, renewal, and the two supersession columns, `SELECT` and `DELETE` on `working_memory`, and `INSERT` on the checkpoint tables; grant the reader `SELECT` on the lease, procedure, and checkpoint tables
    - Revoke `DELETE` on every audit-evidence table from the eraser role, and revoke `UPDATE` and `DELETE` on both checkpoint tables from every role
    - _Requirements: 27.3, 27.4, 27.5, 44.1, 45.9, 46.5, 49.14_

  - [x] 6.8 Write migration 015 for the structural diff summary a redaction leaves
    - Write `src/molt/store/migrations/015_diff_summary.sql` adding the nullable `removed_segments` and `retained_segments` counts to `disposition`, so the redaction comparison view is a query over stored evidence rather than a re-read of a body one side of which no longer exists
    - Keep both columns counts and never text, because a stored diff or a stored list of removed segments would be a copy of the pre-redaction body under another name, and the Disposition table is the one place no body may land
    - Admit absence on both columns, because a hard delete and a retention summarise no rewrite, so a zero there would claim a rewrite dropped nothing rather than record that no rewrite happened
    - Assert non-negativity per column rather than over the pair, and carry each constraint statement in a transaction of its own, because a constraint cannot be dropped and re-added under one name inside the transaction that added the column it reads
    - Grant nothing: the table's privileges are already carried by migration 014, and a column added to an existing table is covered by that table-level grant
    - _Requirements: 18.2, 18.5, 49.14_

  - [x]* 6.9 Write schema and privilege introspection tests for the second generation
    - Create `tests/integration/test_schema_shape_amended.py` asserting the partial `binding_current_unique` index exists and the total pair constraint is gone, the `binding_as_of` index carries its stored columns, the lease current-uniqueness and idempotency indexes exist, `ledger_checkpoint` and `checkpoint_session` exist with no TTL configured, `working_memory` carries the 3600 second expiry default and an hourly TTL job, the confidence equivalence check rejects a Summary carrying a confidence and a Learned_Procedure carrying none, and `artifact_ref` names no working table
    - Assert the Row-Level TTL storage parameters are actually present on `working_memory` by reading the table descriptor back rather than by trusting the configuring statement's outcome, covering the expiration expression, the hourly job cron, and the delete batch size
    - Record the failure mode the read-back guards: setting Row-Level TTL on a table created earlier in the same transaction reports success and commits with the parameters silently absent, leaving a tier that expires no row
    - Create `tests/integration/test_privileges_amended.py` asserting the eraser holds no `DELETE` on any audit-evidence table, no role holds `UPDATE` or `DELETE` on either checkpoint table, the writer's `UPDATE` on `derived_artifact` is confined to the confidence column, and the eraser's `UPDATE` on `erasure_lease` reaches neither owner nor generation
    - _Requirements: 14.7, 14.8, 36.2, 42.9, 42.12, 44.2, 45.9, 45.10, 46.5, 49.1, 49.14_

  - [x]* 6.10 Write the referential action integration tests
    - Create `tests/integration/test_referential_restrict.py` asserting that deleting an Erasure_Run row referenced by a Disposition record is refused with the referencing table name and the referencing row count and that the Disposition record remains present, and covering the same refusal for the request, candidate, residue, run-session, backup, certificate, audit-snapshot, lease, and checkpoint-session references
    - Create `tests/integration/test_referential_cascade.py` asserting that deleting a Derived_Artifact row removes that Artifact's Embedding rows and Lineage_Edges by cascade and that deleting a Session removes its Working_Memory rows
    - _Requirements: 46.1, 46.2, 46.3, 46.6, 46.7_

  - [x] 6.11 Write migration 016 for the two reads the read-only role was never granted
    - Write `src/molt/store/migrations/016_reader_grants.sql` granting the reader role `SELECT` on `erasure_candidate` and `run_session`, the two tables read-only code genuinely reads that fell between the grant list of 007 and the grant list of 014
    - State in the migration that the Sensitivity_Analyzer's residue walk reads the candidate set twice and refuses any role but the read-only one, so the missing privilege is not a shortfall a caller works around by connecting differently
    - State that the Certificate_Verifier joins `run_session` on the deletion arm of the checkpoint accounting query, so the absence of the grant makes every certificate naming a Ledger_Checkpoint unverifiable
    - Grant `SELECT` and nothing further on both tables, because the record of an erasure is evidence and evidence a reader could edit would be worth as little as a certificate a reader could sign
    - Need no guard for re-runnability, and state that in the file: a repeated `GRANT` on this cluster is re-issuable with no second effect
    - Carry the grants in a new file rather than in the grant list they belong to, because the runner refuses a run when a recorded digest no longer matches its file
    - _Requirements: 22.8, 27.5, 27.9, 45.8, 48.5_

  - [x] 6.12 Write migration 017 for the references an authorised erasure has to be able to cut
    - Write `src/molt/store/migrations/017_erasure_references.sql` dropping the Session's spawning Event reference, the Session's parent reference, and the Event's answering-parent reference, each drop guarded so re-application is a clean no-op and each naming both spellings the constraint may carry where the platform may have generated one
    - Keep every dropped column and every index over it, and state in the migration that what goes is the cluster's refusal rather than the record, since the derivation graph is carried independently in `lineage_edge`
    - State the reason in the migration: a sub-agent Session names the Event that spawned it and every Event names the Session it was recorded in, so the pair is a cycle no delete order satisfies, and the two self-references are the same problem inside one table across a batch boundary
    - Leave `ledger.session_id` and `ledger.client_id` enforced, and state why each costs nothing to keep: neither sits in a cycle, and the first is satisfiable by ordering alone
    - Order the hard delete's decisions so that every Event is removed in an earlier batch than, or the same batch as, any Session, with a stable sort so every other Artifact kind keeps the relative order it arrived in
    - Keep the absent-parent refusal by moving it into the write path: guard the inserting Session statement with a join per named row so a parent or spawning row that cannot be found leaves the statement selecting nothing, reported as a missing parent from inside the inserting transaction
    - Carry each statement in a transaction of its own, because a guarded drop naming a constraint an earlier application already removed is checked against state that transaction has not yet been shown
    - _Requirements: 9.7, 18.1, 27.2, 27.13, 46.4, 46.9_

- [x] 7. Implement the Memory_Store data-access layer
  - [x] 7.1 Implement connection handling and the serializable retry wrapper
    - Write `src/molt/store/retry.py` with `in_serializable` retrying the serialization failure state at most 5 times with jittered exponential backoff, then raising `SerializationExhausted` and emitting `store.serialization_exhausted`
    - Write `src/molt/store/__init__.py` with the `MemoryStore` class shell, the `psycopg` pool requiring `sslmode=verify-full`, the 10 second statement timeout, explicit `BEGIN` with `SET TRANSACTION ISOLATION LEVEL SERIALIZABLE` on every write, and bound parameters for every caller-supplied value with no identifier interpolation
    - _Requirements: 15.1, 15.4, 15.5, 30.6, 30.7, 32.6_

  - [x] 7.2 Implement the single-statement hash chain append and the verifier
    - Write `src/molt/store/chain.py` with the one-statement append that derives the next sequence number and predecessor digest from its own read, computes both digests with the cluster's `sha256` over the unit-separated canonical input, and returns the sequence and digests
    - Implement `verify_chain` as an independent Python recomputation reporting the first mismatching sequence number, or the verified row count and terminal digest, and expose a terminal-tip query per Session that the Checkpoint_Signer reads
    - Implement batch append for one Session as a loop of the statement inside one transaction
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 29.7_

  - [x]* 7.3 Write property test for hash chain tamper detection
    - **Property 6: Hash chain tamper detection**
    - **Validates: Requirements 8.2, 8.3, 8.6, 8.7, 22.7**
    - Create `tests/property/test_p06_chain_tamper.py` with the `event_sequences()` generator of 1 to 200 Events plus a mutation selector over payload, category, timestamp, sequence number, content digest, predecessor digest, and chain digest

  - [x]* 7.4 Write property test for hash chain uniqueness under concurrency
    - **Property 7: Hash chain uniqueness under concurrency**
    - **Validates: Requirements 8.1, 8.5, 15.1**
    - Create `tests/property/test_p07_chain_concurrency.py` with the `concurrent_schedules()` generator of 2 to 8 writer tasks and randomised delays, asserting contiguous unique sequence numbers and exactly one predecessor per row

  - [x] 7.5 Implement Session upsert, depth derivation, and counter statements
    - Add `upsert_session` deriving depth from the parent row rather than the caller, `bump_session_counters` as single increment statements, and the terminal-field update path to `src/molt/store/sessions.py`
    - Scope every Session, Event, and Derived_Artifact read query by Client identifier
    - _Requirements: 9.3, 9.4, 9.6, 9.7, 9.8, 15.6, 15.7_

  - [x]* 7.6 Write property test for the Session depth invariant
    - **Property 13: Session depth invariant**
    - **Validates: Requirements 9.3, 9.4, 1.4**
    - Create `tests/property/test_p13_session_depth.py` with the `spawn_trees()` generator of 1 to 30 Sessions to depth 6, exercising both the capture-layer and Collector creation paths

  - [x]* 7.7 Write the counter concurrency suite including the naive comparison
    - Create `tests/concurrency/test_session_counters.py` asserting concurrent counter updates lose no increments under SERIALIZABLE
    - Create `tests/concurrency/test_naive_counter_loss.py` implementing the same update as an application-level read-modify-write and asserting that increments are lost
    - Create `tests/concurrency/test_ledger_appends.py` asserting concurrent appends to distinct Sessions never conflict
    - _Requirements: 15.6, 15.7, 33.1, 36.3, 36.5_

  - [x] 7.8 Implement the lineage queries and the cycle guard
    - Write `src/molt/store/lineage.py` with the guarded insert whose recursive reachability check rejects a cycle-closing edge and whose `artifact_ref` join rejects an absent parent, distinguishing the two causes with one follow-up existence check inside the same transaction
    - Implement the descendant and ancestor recursive common table expressions with `UNION` deduplication, seeded from an array parameter
    - _Requirements: 11.2, 11.3, 11.4, 11.5, 11.6, 11.8_

  - [x]* 7.9 Write property test for lineage descendant closure
    - **Property 4: Lineage descendant closure**
    - **Validates: Requirements 11.5, 11.6, 11.7, 16.5**
    - Create `tests/property/test_p04_lineage_closure.py` with the `dags()` generator up to 500 nodes including diamonds and long chains, compared against an independent Python reference traversal in both directions

  - [x]* 7.10 Write property test for the lineage acyclicity invariant
    - **Property 5: Lineage graph acyclicity invariant**
    - **Validates: Requirements 11.3, 11.4**
    - Create `tests/property/test_p05_lineage_acyclicity.py` with the `edge_insertion_sequences()` generator mixing valid edges, reversed edges, self edges, and edges naming absent identifiers

  - [x] 7.11 Implement embedding writes and the nearest-neighbour query
    - Write `src/molt/store/embeddings.py` writing the Artifact row and its Embedding row in one transaction, writing the provider name alongside the model identifier on every row, validating the 1024 dimension and unit L2 normalisation at write time because the delivered index is `vector_l2_ops` and L2 ordering over unit vectors is cosine ordering, maintaining the `pending`, `embedded`, and `failed` states, and exposing the pending sweep in ascending creation order
    - Implement the one nearest-neighbour statement of the design with the tenancy `EXISTS` filter over a bound Client array restricted to unsuperseded Attribution_Versions, the optional cosine ceiling, `ORDER BY` L2 distance, and the projected cosine distance
    - _Requirements: 10.2, 10.5, 10.6, 10.7, 10.9, 10.10, 37.15, 43.6_

  - [x] 7.12 Implement historical reads with horizon handling
    - Write `src/molt/store/historical.py` composing `AS OF SYSTEM TIME` from a validated timestamp, and raising `HistoricalHorizonError` naming the garbage-collection horizon on a horizon failure with no retry at a different timestamp
    - Read the horizon from the capability record rather than assuming a default, and expose a `within_gc_horizon` predicate that callers consult before attempting a historical read, because the measured horizon of 4500 seconds is shorter than a certificate's evidence lifetime
    - _Requirements: 20.1, 20.5, 20.7_

  - [x] 7.13 Implement the capability probes and the fallback vector path
    - Add `capabilities()` to `MemoryStore` reading and caching the capability record at process start, covering the vector index and its reported operator class, changefeed availability, the rangefeed setting, the measured garbage-collection horizon, the self-managed backup path, the absence of on-demand backup creation, and the text provider prompt-cache capability
    - Write the zone-configuration probe recording the measured horizon and the `BACKUP INTO` probe recording the self-managed path, so the Backup_Manager's path choice is driven by a probe result rather than by a version string
    - Switch the nearest-neighbour statement between the index-served form, which is the expected path on the delivered cluster, and the bounded exact-scan fallback for tiers where the index cannot be created, with the same SQL shape, the same cosine thresholds, a client restriction, an explicit row cap, and `store.vector_index_unavailable` emitted
    - _Requirements: 10.3, 10.11, 13.5, 19.5, 20.7, 32.1_

  - [x]* 7.14 Write unit tests for the data-access functions
    - Create `tests/unit/test_store_statements.py` driving the append, lineage, binding, embedding, and historical statement builders against a stub cursor, asserting bound parameters only and no identifier interpolation
    - _Requirements: 30.6, 36.1_

  - [x]* 7.15 Write the store integration suite
    - Create `tests/integration/test_vector_search.py`, `test_lineage_ctes.py`, `test_historical_reads.py`, and `test_transaction_atomicity.py` covering index-served nearest-neighbour search, the recursive traversals, `AS OF SYSTEM TIME` reads inside the horizon and a request beyond the horizon returning the named error, and atomicity of Artifact-plus-Embedding and Artifact-plus-Attribution writes
    - Create `tests/integration/test_fallback_vector_scan.py` asserting the fallback exact-scan path returns the same ordering and threshold behaviour as the index-served path
    - _Requirements: 10.3, 10.5, 10.11, 11.5, 20.1, 20.5, 20.6, 32.1, 36.2_

  - [x]* 7.16 Write property test for hygiene detection and parameter binding
    - **Property 39: Metadata-hygiene detection and parameter binding**
    - **Validates: Requirements 29.4, 29.8, 30.6, 36.16**
    - Create `tests/property/test_p39_hygiene_and_binding.py` with the `hygiene_fixtures()` and `adversarial_values()` generators, asserting the check names the pattern class and exits non-zero on prohibited content and exits 0 on allowlisted-only content, and that strings carrying quotes, semicolons, comment markers, and statement fragments round-trip through Memory_Store as data and alter no schema or query semantics

  - [x]* 7.17 Write the lineage traversal performance test
    - Create `tests/perf/test_lineage_scale.py` generating a 100000-edge graph and asserting both traversals terminate within 5 seconds
    - _Requirements: 11.7_

- [x] 8. Implement the attribution, working, and fencing store modules
  - [x] 8.1 Implement bitemporal Attribution_Version storage
    - Write `src/molt/store/attribution.py` with the first-write insert path guarded by the current-version partial unique index, the atomic supersession statement pair that closes the current version, inserts the successor carrying the greater of the submitted and prior confidence, and appends one `attribution_superseded` Ledger Event, all in one SERIALIZABLE transaction
    - Shape the supersession as two ordered statements rather than one: first close the current version by setting its validity end timestamp and its superseding reference to the successor's generated identifier, then insert the successor, both inside that one SERIALIZABLE transaction, with no common table expression and no self-referencing foreign key behind the superseding reference
    - Implement the current-attribution query returning only versions carrying no superseding reference and the as-of-attribution query over the half-open validity interval ordered by Client, plus the earliest-version-per-Artifact query the certificate reads before any disposition runs
    - Treat detection method, confidence, Artifact identifier, and Client identifier as immutable on a stored version, raising `AttributionImmutable` on any restatement attempt, and route the surgical-redaction binding removal through a closing supersession that records the erasure rather than through a delete
    - Move every binding read to the current-attribution form: the explicit sweep, the disposition classification join, the parent-pruning subquery, the recall tenancy filter, the nearest-neighbour filter, and the MCP tenancy filter
    - Emit `attribution.supersessions` on each supersession
    - _Requirements: 12.7, 43.1, 43.2, 43.3, 43.4, 43.5, 43.6, 43.7, 43.8, 43.12, 43.13_

  - [x]* 8.2 Write property test for the Client binding uniqueness invariant
    - **Property 14: Client binding uniqueness invariant**
    - **Validates: Requirements 12.5, 12.7, 43.5**
    - Create `tests/property/test_p14_binding_uniqueness.py` with the `binding_write_sequences()` generator of 1 to 20 repeated writes for one Artifact and Client with confidences drawn from the unit interval and methods drawn from the method set, asserting exactly one unsuperseded version holding the maximum submitted confidence and every stored confidence inside the closed unit interval

  - [x]* 8.3 Write property test for attribution history correctness
    - **Property 32: Attribution history correctness**
    - **Validates: Requirements 43.1, 43.2, 43.3, 43.4, 43.5, 43.8, 12.7**
    - Create `tests/property/test_p32_attribution_history.py` with the `attribution_write_sequences()` generator of 1 to 50 writes per Artifact over 2 to 5 Clients including repeated identical writes, monotone confidence runs, and method changes at equal confidence, crossed with as-of timestamps drawn from before the first write, at each supersession instant, between supersessions, and after the last write, asserting half-open interval containment, the current-query result set, immutability of every stored version's method, confidence, Artifact, and Client, and exactly one Ledger Event per supersession naming both version identifiers

  - [x]* 8.4 Write the attribution integration and concurrency tests
    - Create `tests/integration/test_attribution_supersession.py` asserting the closure and the insert commit together, that the Ledger Event is present in the same transaction, and that the as-of query answers within 1 second for an Artifact carrying at least 100 Attribution_Versions
    - Create `tests/concurrency/test_attribution_race.py` asserting that two concurrent supersessions for the same pair leave exactly one unsuperseded version and that the loser retries against the new current version
    - _Requirements: 43.3, 43.4, 43.10, 36.2_

  - [x] 8.5 Implement the working memory tier accessor
    - Write `src/molt/store/working.py` with the upsert on the Session and scratch-key primary key, the single point read, the per-Session listing, and the one set-based per-Client purge statement returning the aggregate deleted row count that the Erasure_Engine records on the run row
    - Set the expiry timestamp from the configured 3600 second interval on every write, reference no working row from any other table, and expose no path by which a working row becomes a lineage parent, an erasure candidate, or a binding subject
    - Emit `erasure.working_rows_deleted` with the aggregate count
    - _Requirements: 42.7, 42.8, 42.9, 42.12, 42.13_

  - [x] 8.6 Implement the fencing generation storage and the guarded write predicate
    - Write `src/molt/store/fencing.py` with the current-generation read for a Client and the guarded write predicate that reads the current Fencing_Generation in the same transaction as the write and admits the write only when the presented generation matches
    - Implement `fenced`, wrapping a write body in the serializable retry wrapper behind that predicate, raising `StaleFencingGeneration` carrying the presented and the current generation, persisting no row, and emitting `erasure.stale_generation_refused`
    - Expose the wrapper for the Disposition write, the run completion record, and the certificate insert, so a superseded owner can neither record evidence, declare a run finished, nor sign for it
    - _Requirements: 44.7, 44.8, 44.15_

  - [x]* 8.7 Write the working tier integration test
    - Create `tests/integration/test_working_tier.py` asserting an upsert overwrites in place, a point read returns the stored value, the per-Client purge removes every row for that Client in one statement and returns the count, row-level TTL physically removes an expired row with no process outside the cluster, and no table other than `working_memory` holds a reference to a working row
    - _Requirements: 42.9, 42.12, 42.13, 36.2_

- [x] 9. Checkpoint - store verified
  - Ensure all tests pass and both migration generations re-apply cleanly, ask the user if questions arise.

- [x] 10. Implement the capture layer
  - [x] 10.1 Implement the bounded spool
    - Write `src/molt/capture/spool.py` writing one JSON record per line to the configured spool directory, opened for append so concurrent hook processes interleave at record granularity
    - Implement head-dropping at the 64 MiB bound by streaming surviving records into a sibling temporary file and renaming it over the original, and record the discarded count in a counter file for reporting on the next successful transmission
    - Hold records rather than signed requests, so a spooled batch is signed with a fresh timestamp at transmission and a long spool outage produces no batch rejected as stale
    - _Requirements: 6.1, 6.5, 47.10_

  - [x] 10.2 Implement the hook entry point, signed transmission, and halt observation
    - Write `src/molt/capture/hook.py` with `main` wrapped in a total exception handler that catches every failure including non-UTF-8 decoding, writes at most one diagnostic line to standard error, and exits 0 on every input
    - Implement workspace-to-Client resolution falling back to the reserved `unassigned` Client with a warning on standard error, spool-first transmission on start, at most 3 retries with 200, 400, and 800 millisecond backoff plus jitter, a 5 second cap on every network operation, a soft deadline after which the batch is spooled, and no database driver import in the hook process
    - Read the ingress shared secret from configuration, compute the HMAC-SHA256 signature over the presented timestamp concatenated with the exact serialised body bytes immediately before transmission, and present the timestamp header and the signature header on every request to the Event batch and Session metadata endpoints; on an absent shared secret write one diagnostic line, spool rather than transmit, and exit 0 while leaving the recall path unaffected
    - Read the halt, halt reason, and pending approval fields from the Collector response envelope, return the adapter blocking response and queue a policy halt Event when halted or when a pending approval matches the current action, and record the unobserved-halt condition in the diagnostic line when the Collector is unreachable
    - _Requirements: 1.2, 1.3, 1.5, 1.6, 1.7, 6.1, 6.2, 6.3, 6.4, 6.6, 13.8, 23.7, 23.9, 47.10_

  - [x] 10.3 Implement the five per-tool hook adapters
    - Write `src/molt/capture/adapters/claude_code.py`, `cursor.py`, `codex.py`, `gemini_cli.py`, and `copilot.py`, each implementing the adapter protocol from that tool's own published specification, sharing only the Event builders
    - Implement per adapter: the vendor hook event to Event category mapping table of the design, subagent parent Session and spawning Event population, correlation-identifier linkage through the parent Event identifier with the local invocation index fallback, the context-injection envelope, the blocking response, the empty-result no-op response, and the capability flags for structured output, context injection, and blocking decision
    - Write the shim dispatch so the invoking hook format identifies the Agent_CLI, with an unknown token still exiting 0
    - Record the consumed field names per tool in the notes written by task 33.3
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.9, 7.8, 13.6, 29.9_

  - [x]* 10.4 Write property test for capture never failing the host agent
    - **Property 22: Capture never fails the host agent**
    - **Validates: Requirements 1.7, 6.6, 4.1**
    - Create `tests/property/test_p22_capture_exit_zero.py` with the `hook_inputs()` generator: valid payloads per adapter, truncated JSON, wrong-type fields, absent fields, 1 MiB fields, non-UTF-8 sequences, empty input, and an absent configuration environment, asserting exit 0, no unredacted secret, and at most one diagnostic line per failure

  - [x] 10.5 Implement the MCP proxy
    - Write `src/molt/capture/mcp_proxy.py` with stdio and HTTP transports, forwarding every frame byte-for-byte excluding transport framing, parsing a copy of the frame for observation only, linking request and response Events by the JSON-RPC identifier through the parent Event identifier, incrementing a dropped-Event counter on emission or parse failure without touching the relay path, and closing the downstream connection with a session end Event on upstream close
    - Present the ingress timestamp and signature headers on every ingest request the proxy makes, using the same signing helper the hook uses
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 47.10_

  - [x]* 10.6 Write property test for MCP proxy transparency
    - **Property 24: MCP proxy transparency**
    - **Validates: Requirements 2.4, 2.2, 2.3**
    - Create `tests/property/test_p24_mcp_transparency.py` with the `jsonrpc_sequences()` generator covering request, response, notification, and batch forms plus non-JSON bodies and frames at the size cap

  - [x] 10.7 Implement the decorator API
    - Write `src/molt/capture/decorator.py` with the tool decorator emitting a tool call Event before and a tool result Event after the call, recording duration in milliseconds, converting an exception into an error Event carrying the type and redacted message before re-raising the original, the session context manager opening and closing a Session, and pass-through behaviour when Molt is unconfigured
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

  - [x]* 10.8 Write unit tests for the adapters, proxy, decorator, and ingress signing
    - Create `tests/unit/test_adapters.py` asserting the mapping table per adapter, capability flags, injection and blocking envelopes, and subagent field population
    - Create `tests/unit/test_decorator.py` and `tests/unit/test_spool.py` covering exception re-raise with the error Event, unconfigured pass-through, and spool head-dropping with the discarded count
    - Create `tests/unit/test_ingress_signing.py` asserting the signed material is the timestamp concatenated with the exact body bytes, that the signer and the verifier insert the same separators, and that a spooled batch is signed with a fresh timestamp at transmission
    - _Requirements: 1.2, 1.4, 3.1, 3.2, 3.5, 6.5, 36.1, 47.2, 47.10_

  - [x]* 10.9 Write the hook latency performance test
    - Create `tests/perf/test_hook_latency.py` driving 1000 invocations of the real entry point against a local stub Collector, including the signing step, and asserting the 250 millisecond p95 bound
    - _Requirements: 1.8_

- [x] 11. Implement the Collector with signed ingress
  - [x] 11.1 Implement the handler, routing, bearer authentication, and the request bound
    - Write `src/molt/collector/handler.py` and `routes.py` with the Event batch endpoint, the Session metadata endpoint, the recall endpoint, and the unauthenticated health endpoint reporting liveness, database reachability, the capability record summary, and no memory content
    - Compare the bearer token with a constant-time comparison, respond 401 on absent or mismatched tokens, persist every well-formed record in a partly malformed batch and return accepted and rejected counts, create an absent Session in the same transaction as its Event, read the connection string, expected bearer token, and ingress shared secret from Parameter_Store at cold start and cache them for the container lifetime, and respond 503 with `collector.write_failure` when the cluster is unreachable
    - Read the declared body length before any decode and reject a body exceeding the configured maximum of 5 MiB with status 413, persisting no record from that request including no record from a well-formed prefix
    - Read the reserved concurrency ceiling of 10 from the configuration surface so the deployment template in task 29.3 declares the same value, and return the halt, halt reason, and pending approval fields in every ingest and recall response envelope
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9, 5.10, 5.11, 5.12, 30.3, 30.5, 32.6_

  - [x] 11.2 Implement Ingress_Signature verification and its rejection paths
    - Write `src/molt/collector/ingress.py` with `verify_ingress` reading the timestamp header and the signature header before any body handling, recomputing the HMAC-SHA256 digest over the presented timestamp concatenated with the raw request body bytes taken before any decode, and comparing with a constant-time comparison
    - Enforce the age bound: reject when the absolute difference between the cluster's current timestamp and the presented timestamp exceeds the configured maximum request age of 300 seconds
    - Respond 401 persisting no record on signature mismatch, on an out-of-window timestamp, on an absent timestamp header, and on an absent signature header, running the whole check before the transaction opens so no partial write is possible for a batch whose leading records are well-formed, and emit `collector.signature_rejected` on each of those four causes
    - Require the signature in addition to the bearer token on the Event batch and Session metadata endpoints only, leaving the recall endpoint bearer-only so an interactive caller holding no shared secret still reaches the recall path
    - _Requirements: 5.13, 47.1, 47.2, 47.3, 47.4, 47.5, 47.6, 47.7, 47.8, 47.9, 47.11, 47.12, 47.13_

  - [x]* 11.3 Write property test for Collector partial-batch acceptance
    - **Property 23: Collector partial-batch acceptance**
    - **Validates: Requirements 5.6**
    - Create `tests/property/test_p23_partial_batch.py` with the `batches()` generator of 1 to 200 records mixing valid Event JSON with truncated lines, wrong-type fields, empty lines, and oversized lines

  - [x]* 11.4 Write property test for the Collector request bound
    - **Property 29: Collector request bound**
    - **Validates: Requirements 5.10, 5.11**
    - Create `tests/property/test_p29_request_bound.py` with the `request_bodies()` generator producing byte lengths well below, one byte below, exactly at, one byte above, and far above the configured maximum, including an oversized body whose leading records are all well-formed, asserting 413 and no persisted record for every oversized body

  - [x]* 11.5 Write property test for ingress signature verification
    - **Property 34: Ingress signature verification**
    - **Validates: Requirements 47.1, 47.2, 47.4, 47.5, 47.7, 47.8**
    - Create `tests/property/test_p34_ingress_signature.py` with the `signed_requests()` generator of bodies from 0 to 5 MiB including the empty body, crossed with timestamps offset from the injected cluster clock by values straddling the configured maximum request age in both directions, crossed with an alteration selector over body mutation, signature mutation, timestamp-header removal, signature-header removal, and no alteration, asserting acceptance only for a correctly signed in-window request and 401 with no persisted record for every other case including the well-formed prefix of an otherwise valid batch

  - [x]* 11.6 Write Collector integration tests
    - Create `tests/integration/test_collector_ingest.py` asserting Session creation inside the Event transaction, 401 on a bad token, 401 on each of the four signature rejection causes with nothing persisted, 413 with nothing persisted on an oversized body, 503 with the failure metric on an unreachable cluster, a health body carrying no memory content, and the recall endpoint succeeding with the bearer token alone
    - _Requirements: 5.3, 5.4, 5.7, 5.9, 5.11, 47.4, 47.7, 47.8, 47.12, 36.2_

  - [x]* 11.7 Write the ingress replay security test
    - Create `tests/security/test_ingress_replay.py` asserting a captured request is accepted once inside the age bound and rejected once the bound has passed, driving the clock through the injected time source rather than by waiting
    - _Requirements: 47.5, 47.6, 47.14_

  - [x]* 11.8 Write the ingest rate performance test
    - Create `tests/perf/test_ingest_rate.py` asserting a sustained rate of at least 100 Events per second with signature verification on the path
    - _Requirements: 33.2_

- [x] 12. Implement the Embedder, the Binding_Detector, and the Recall_Engine
  - [x] 12.1 Implement the Embedder against the provider abstraction
    - Write `src/molt/embed/__init__.py` calling the Embedding_Provider obtained from the Provider_Selector and never a provider SDK directly, batching at most 25 texts per provider call, L2-normalising every vector before write, retrying at most 3 times with exponential backoff and then leaving the Artifact in the pending state, writing the selected provider name alongside the model identifier on every Embedding row, and draining pending Artifacts in ascending creation order
    - Include a deliberately non-unit-normalised Embedding_Provider stub among the provider stubs the property test of task 12.2 draws from, so the normalising step is exercised rather than bypassed
    - Record the reason in the stub's own description: the vector index orders by L2 distance while the thresholds are cosine, and the two coincide only on unit vectors, and the delivered external embedding provider already returns normalised vectors, which would otherwise leave the normalisation untested
    - _Requirements: 10.1, 10.8, 10.10, 10.14, 32.1, 32.2, 33.7, 34.3, 37.5, 37.15_

  - [x]* 12.2 Write property test for provider substitutability
    - **Property 26: Provider substitutability**
    - **Validates: Requirements 37.5, 37.8, 37.9, 37.15, 10.2, 10.10**
    - Create `tests/property/test_p26_provider_substitutability.py` with the `provider_inputs()` generator of text 1 to 8192 characters including source-code shaped fragments, non-ASCII text, and whitespace-only input, crossed with `provider_stubs()` reporting each configured implementation's real declared width and normalisation behaviour plus a mismatched-width stub for the rejection edge case, asserting 1024 dimensions, unit L2 norm within tolerance, byte-identical schema and nearest-neighbour query text across selections, and the provider name written on the Embedding row

  - [x] 12.3 Implement the Binding_Detector on the supersession path
    - Write `src/molt/store/binding_detector.py` emitting scope bindings at confidence 1.0 for the owning Session's Client, inherited bindings at the parent binding confidence for every Client bound to any parent, and marker bindings at confidence 0.9 for every Client whose configured content markers appear in the Artifact text, all written in the Artifact's transaction
    - Route every write through the attribution module: a new pair is a plain insert, and a differing method or confidence for an existing pair is a supersession rather than an overwrite, so the maximum-confidence rule operates on the unsuperseded version
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 43.3_

  - [x]* 12.4 Write property test for binding inheritance monotonicity
    - **Property 15: Binding inheritance monotonicity**
    - **Validates: Requirements 12.3**
    - Create `tests/property/test_p15_binding_inheritance.py` with the `derivation_chains()` generator over 2 to 5 Clients, asserting the child Client set is a superset of the union of parent Client sets

  - [x] 12.5 Implement the Recall_Engine with confidence weighting
    - Write `src/molt/recall/__init__.py` performing one embedding call through the configured provider, one nearest-neighbour query with the tenancy filter applied in SQL over unsuperseded Attribution_Versions from the authenticated principal mapping rather than the request body, and one join to the originating Session for outcome, machine identifier, and timestamp
    - Order by ascending cosine distance with the Procedure_Confidence descending tie-break and the Artifact identifier as the final key so ordering is total, truncate to k, and append one recall Event recording query text, returned identifiers, and distances after the response is composed
    - Apply the recall floor inside the SQL predicate rather than after truncation so a low-standing Learned_Procedure does not shrink a k-result page, retain the row in storage, emit `procedure.recall_floor_exclusions`, and return an empty result set when the cluster is unreachable
    - Record one retrieval through the Confidence_Tracker per returned Learned_Procedure, off the latency path
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.7, 13.8, 43.6, 49.3, 49.8, 49.9, 49.10_

  - [x]* 12.6 Write property test for recall tenancy filtering
    - **Property 16: Recall tenancy filtering**
    - **Validates: Requirements 13.2, 13.3, 13.4, 9.8**
    - Create `tests/property/test_p16_recall_tenancy.py` with the `corpora_with_permissions()` generator of 50 to 500 stub embeddings across 2 to 5 Clients plus a permitted subset, asserting every result carries a permitted binding and that the Session identifier, machine identifier, timestamp, and outcome match the stored row

  - [x]* 12.7 Write property test for recall ordering
    - **Property 17: Recall ordering**
    - **Validates: Requirements 13.1, 10.7**
    - Create `tests/property/test_p17_recall_ordering.py` reusing `corpora_with_permissions()` with Learned_Procedures carrying deliberate ties at equal distance, asserting non-decreasing distances, the confidence tie-break ordering, a result count at most k, and no repeated Artifact

  - [x]* 12.8 Write the recall performance test in both index modes
    - Create `tests/perf/test_recall_latency.py` asserting the 2 second p95 bound over at least 100000 embeddings index-served and recording the fallback exact-scan figure for the documentation
    - _Requirements: 10.3, 10.11, 13.5_

  - [x]* 12.9 Write the provider service integration tests
    - Create `tests/integration/services/test_providers.py` with one representative call per implementation per role: a Bedrock embedding call and a Bedrock text call, and an external embedding call and an external text call, each skipped with a clear message when that provider is unavailable on the account so no other suite depends on it
    - Assert the returned width is exactly 1024 for each embedding implementation and that each text call completes; assert the text calls report the cache-creation and cache-read token fields in the usage response so the fields the Adjudicator records are known to exist
    - Assert the retry count is bounded at 3 with a counting stub and that batching caps at 25 texts
    - _Requirements: 10.1, 10.8, 33.7, 34.11, 36.15, 37.3, 37.4, 38.5_

- [x] 13. Implement the Confidence_Tracker
  - [x] 13.1 Implement retrieval records, outcome records, the bounded adjustment, and the change history
    - Write `src/molt/confidence/__init__.py` with `record_retrieval` writing one row per returned procedure per consuming Session without moving the value, `record_outcome` sourcing the classification from the Session's own terminal outcome joined to that Session's retrieval records so an outcome cannot be asserted for a procedure the Session never retrieved, `history` returning the change records ordered by change timestamp, and `summary` returning confidence, retrieval count, and outcome counts per classification
    - Set the initial value from the configured default of 0.5 in the writing statement for every Derived_Artifact of the Learned_Procedure kind, so a procedure never exists without a standing
    - Apply the configured increment of 0.05 on a succeeded outcome, the configured decrement of 0.10 on a failed outcome, and no adjustment and no change record on an abandoned outcome, clamping to the closed unit interval inside the SQL statement with the column check as the backstop
    - Write the adjusted value and its change record as one statement pair in one SERIALIZABLE transaction, skipping the change record when the clamped value did not move, and emit `procedure.confidence_changes` with the direction dimension
    - _Requirements: 49.1, 49.2, 49.3, 49.4, 49.5, 49.6, 49.7, 49.12, 49.13, 49.15_

  - [x]* 13.2 Write property test for procedure confidence bounds and direction
    - **Property 36: Procedure confidence bounds and direction**
    - **Validates: Requirements 49.1, 49.5, 49.6, 49.7, 49.12, 49.13**
    - Create `tests/property/test_p36_procedure_confidence.py` with the `procedure_event_sequences()` generator of 1 to 200 retrieval and outcome events per Learned_Procedure across the three classifications, with runs long enough to drive the value to both bounds and to attempt adjustments past them, plus duplicate outcomes for one Session so the per-Session uniqueness constraint is exercised, asserting the closed unit interval after every event, upward movement on succeeded, downward on failed, equality on abandoned, and a change-record count equal to the count of events that changed the value with prior and new values matching each transition

  - [x]* 13.3 Write the confidence integration test
    - Create `tests/integration/test_confidence_transaction.py` asserting the value change and its change record commit together and neither exists without the other, that a concurrent adjustment aborts one transaction whose retry re-reads the current value and writes a record matching the transition it actually caused, that a procedure below the floor is retained in storage, and that the ordered change-history query returns the records in change order
    - _Requirements: 49.10, 49.13, 49.15, 36.2_

  - [x]* 13.4 Write unit tests for the delta arithmetic and clamping
    - Create `tests/unit/test_confidence_arithmetic.py` asserting the increment and decrement values, clamping at both bounds, no change record at a clamped bound, and no adjustment on abandoned
    - _Requirements: 49.5, 49.6, 49.7, 49.12, 36.1_

- [x] 14. Checkpoint - providers, capture, ingest, recall, and procedural standing verified
  - Ensure all tests pass, ask the user if questions arise.

- [x] 15. Implement the Lease_Manager and fenced erasure ownership
  - [x] 15.1 Implement lease grant, refusal, renewal, takeover, and idempotent finalisation
    - Write `src/molt/erase/lease.py` with `acquire`, `renew`, `release`, `current`, and `finalisation_for`, assigning the Fencing_Generation as the highest generation previously recorded for that Client plus one, computed and inserted in one SERIALIZABLE transaction so a racing second commit aborts and its retry finds a current lease and is refused rather than granted a duplicate generation
    - Refuse an acquisition from a different owner while a lease is current, reporting the current owner identifier and the current Fencing_Generation as `LeaseRefused`, and extend the expiry by the configured lease interval of 30 seconds on renewal
    - Admit takeover only once the expiry timestamp precedes the cluster's current timestamp, evaluated against the cluster clock inside the transaction and never against a worker's local clock, incrementing the generation by the same rule as a first grant and emitting `erasure.lease_takeovers`
    - Shape every lease supersession as two ordered statements rather than one: first close the current lease by setting its supersession timestamp and its superseding lease reference to the successor's generated identifier, then insert the successor lease, both inside one SERIALIZABLE transaction, with no common table expression and no self-referencing foreign key behind the superseding reference
    - Record one idempotency key per run and return the recorded finalisation result performing no mutation when that key is already marked finalised
    - Leave a lease to expire rather than releasing it on an abort, so a crashed worker and a cleanly aborted worker both release ownership on the cluster's clock
    - _Requirements: 44.1, 44.2, 44.3, 44.4, 44.5, 44.6, 44.9, 44.10, 44.16, 44.17, 44.18_

  - [x] 15.2 Wire fenced writes into every evidence path
    - Route the Disposition write, the run completion record, and the certificate insert through the fenced wrapper of task 8.6, carrying the writing owner's Fencing_Generation on each, and carry the finalising generation onto the certificate row so the fencing claim is auditable from the document
    - Raise `LeaseNotHeld` and abort before any mutation when a run begins for a Client while holding no current lease, reporting the current owner identifier
    - Abort the run with the aborted status when a mid-run write is refused as stale, retaining the evidence written under the valid generation
    - _Requirements: 44.7, 44.8, 44.11, 44.12, 44.15_

  - [x]* 15.3 Write property test for fencing safety under contention
    - **Property 31: Fencing safety under contention**
    - **Validates: Requirements 44.2, 44.3, 44.4, 44.6, 44.7, 44.8, 44.12**
    - Create `tests/property/test_p31_fencing_safety.py` with the `lease_schedules()` generator of 2 to 20 worker identities contending for 1 to 3 Clients, crossed with an operation sequence over acquire, renew, let-expire, take-over, terminate-abruptly, revive, and attempt-disposition-write, with the lease interval drawn small enough that expiry occurs inside the example and the clock advanced through the injected time source rather than by sleeping, asserting at most one current generation per Client at every point, refusal with `stale_fencing_generation` and no persisted row for every non-current generation, strictly increasing takeover generations, no takeover granted before expiry, and no mutation from a run begun with no held lease

  - [x]* 15.4 Write the lease contention concurrency demonstration
    - Create `tests/concurrency/test_lease_contention.py` driving at least 10 real worker processes: assert exactly one grant and nine or more refusals each naming the same current owner and generation, terminate the winner abruptly with no release and no final renewal, assert a second worker's acquisition is refused while the expiry is still in the future and succeeds with an incremented generation once it has passed, then revive the terminated worker, have it attempt a disposition write with the generation it still believes it holds, and assert the refusal names both generations, that no disposition row was persisted, and that the refusal metric was emitted
    - _Requirements: 44.13, 44.15, 36.4_

  - [x]* 15.5 Write unit tests for the generation arithmetic and refusal messages
    - Create `tests/unit/test_lease_arithmetic.py` asserting the generation is the per-Client historical maximum plus one across superseded leases, that a refusal message carries the current owner and generation and no secret, and that a repeated finalisation returns the recorded result unchanged
    - _Requirements: 44.3, 44.4, 44.10, 36.1_

- [x] 16. Implement the erasure engine
  - [x] 16.1 Implement phase one, the explicit sweep
    - Write `src/molt/erase/sweep.py` with the five set-based statements recording their selection reasons for Sessions owned by the Client, Events of those Sessions, every Artifact carrying a current Attribution_Version for the Client, lineage descendants of everything selected, and Embeddings of everything selected, plus the run-session chain-tip capture statement
    - Resolve the binding selection through the current-attribution query so a superseded version never widens or narrows the sweep, record the pending-embedding Artifact count on the run row, and include every Learned_Procedure whose Procedure_Confidence is below the configured recall floor in the candidate set
    - _Requirements: 10.9, 16.1, 16.2, 16.3, 16.4, 16.5, 16.6, 16.7, 21.9, 43.6, 49.11_

  - [x] 16.2 Implement phase two, the Residue_Detector
    - Write `src/molt/erase/residue.py` selecting query Artifacts by descending text length within kind up to the configured limit, running the index-served nearest-neighbour query at the review threshold, anti-joining the candidate set in SQL as well as in the loop, keeping the smallest distance per Artifact, and recording distance, band, threshold comparison, inclusion, and decision reason per candidate
    - Group candidates by query Artifact before dispatch to the Adjudicator, so calls sharing a Stable_Prefix are issued together rather than interleaved with calls carrying a different prefix
    - Expose the same path in read-only mode for the CLI residue verb, the Sensitivity_Analyzer, and the MCP residue tool, against a synthetic run row that mutates no memory content
    - _Requirements: 17.1, 17.2, 17.3, 17.4, 17.7, 17.9, 17.10, 38.2_

  - [x] 16.3 Implement the Adjudicator with its prompt structure and its fail-closed path
    - Write `src/molt/erase/adjudicator.py` calling the configured Text_Provider once per review-band candidate with bounded concurrency, recording the provider name, the model identifier, the prompt digest, the classification, and the reasoning text per adjudicated candidate
    - Implement the Stable_Prefix as the task instructions followed by the length-capped query Artifact excerpt under the configured prefix byte budget of 32768 bytes by default, memoised on the query Artifact identifier for the lifetime of the run so the serialised prefix is byte-identical for every candidate sharing that query Artifact, with nothing that varies per candidate appearing in it
    - Place the candidate excerpt in the variable suffix after the prefix and set the Cache_Boundary from the recorded prompt-cache capability, so a provider supporting caching gets the boundary marked immediately after the prefix and a provider without caching gets the same two-part structure with no marker
    - Apply the prompt-cache floor as well: mark the Cache_Boundary only when the Stable_Prefix length reaches the configured `Minimum_Cacheable_Prefix_Length`, whose default is 16384 bytes, and below that floor send the identical two-part prompt structure unmarked, because a cache write carrying no subsequent cache read costs more than no caching
    - Record the cache-creation and cache-read token counts per adjudication batch from the result usage fields
    - Implement the fail-closed path: throttling after retries, timeout, unparseable response, or credential failure classifies the candidate as included with the fail-closed reason and the adjudicated flag false, and emits the fail-closed metric
    - _Requirements: 17.5, 17.6, 17.8, 38.1, 38.2, 38.3, 38.4, 38.5, 38.7, 38.8, 38.9, 38.10_

  - [x]* 16.4 Write property test for prompt cache prefix stability
    - **Property 30: Prompt cache prefix stability**
    - **Validates: Requirements 38.1, 38.2, 38.3**
    - Create `tests/property/test_p30_prompt_cache_prefix.py` with the `candidate_sets()` generator of one query Artifact excerpt crossed with 2 to 50 candidate excerpts of varying length and content, including excerpts containing the prefix's own text, plus a provider-capability selector toggling prompt-cache support, asserting the serialised Stable_Prefix is byte-identical across the set and each Cache_Boundary falls exactly at the end of that prefix

  - [x]* 16.5 Write property test for residue candidate disjointness and recovery
    - **Property 18: Residue candidate disjointness and recovery**
    - **Validates: Requirements 17.2, 17.3, 17.4, 17.7**
    - Create `tests/property/test_p18_residue_disjointness.py` with the `contaminated_corpora()` generator placing stub vectors at controlled distances straddling both thresholds, asserting disjointness, recovery of every planted fragment at or below the auto-inclusion threshold, no Adjudicator call below that threshold, and a distance, band, and reason on every recorded candidate

  - [x] 16.6 Implement the Redaction_Rewriter with validation and its fail-closed path
    - Write `src/molt/erase/rewriter.py` calling the configured Text_Provider once per blended Artifact and validating the replacement: non-empty after stripping, no occurrence of the erased Client's slug, display name, or content markers, length inside the configured ratio band, and a retained Client's marker still present when the original carried one
    - Collapse every provider failure cause and every validation failure to unavailability, and emit the redaction fail-closed metric
    - _Requirements: 18.3, 18.7_

  - [x] 16.7 Implement phase three, per-artifact disposition
    - Write `src/molt/erase/disposition.py` with the classification statement over the candidate set joined to current Attribution_Versions only, the decision table producing hard delete, surgical redaction, the non-divisible Event hard delete, and retained with a reason, the ordered batch delete transaction capturing pre-deletion digests and binding slugs, and the single surgical transaction with the optimistic content-digest guard, edge removal for parents whose current attributions name the erased Client alone, a closing supersession that records the binding removal as history rather than a hole, the replacement embedding, the stored structural diff summary, and the disposition row
    - Retain both digests, store no pre-redaction body anywhere, and carry the writing owner's Fencing_Generation on every disposition write
    - _Requirements: 18.1, 18.2, 18.4, 18.5, 18.6, 18.7, 18.8, 43.1, 44.7_

  - [x] 16.8 Implement the Backup_Manager with its primary and fallback paths
    - Write `src/molt/backup/__init__.py` with `take_backup` choosing the path from the capability record: the primary self-managed path issues a `BACKUP INTO` statement against the operator-owned bucket before the first mutation of the run and records the target URI, the exact statement issued, the timestamp, the self-managed path value, taken true, and referenced false
    - Implement the managed-reference fallback entered only when the capability record reports the self-managed path unavailable: retrieve the most recent Managed_Backup identifier and timestamp through the ccloud CLI invoked as a subprocess with an argument vector and never a shell string, and record the exact command vector, the managed-referenced path value, taken false, and referenced true
    - Record the failed status with detail when no path succeeds, returning a failure the engine treats as fatal before any mutation, and the skipped status when the operator passes the skip flag
    - _Requirements: 19.1, 19.2, 19.3, 19.4, 19.5, 19.6, 19.7_

  - [x]* 16.9 Write property test for backup path recording agreement
    - **Property 27: Backup path recording agreement**
    - **Validates: Requirements 19.2, 19.3, 19.5, 19.6, 19.7, 21.7**
    - Create `tests/property/test_p27_backup_path_agreement.py` with the `backup_scenarios()` generator of Erasure_Runs over `memory_graphs()` crossed with a capability selector marking the self-managed path available or unavailable, a failure selector making zero, one, or both paths fail, and the skip flag present and absent, asserting the recorded path value, the taken and referenced flags, and the certificate's backup evidence all name the path actually taken, that no run records a taken backup when none succeeded, and that a run with no successful path and no skip flag aborts with every memory-content table unchanged

  - [x] 16.10 Implement the Erasure_Engine orchestration under a held lease
    - Write `src/molt/erase/engine.py` acquiring the Erasure_Lease before any mutation and aborting with the current owner reported when no lease is granted, then executing the phases with the transaction boundaries of the design, holding no transaction open across a model or subprocess call, renewing the lease while the run is in flight, and running every transaction through the serializable retry wrapper with replayable bodies
    - Issue the single set-based working-memory delete for the Client at run start and record the returned count on the run row as one aggregate number rather than as per-row Dispositions
    - Abort before any mutation on backup failure, batch dispositions at 100, emit phase progress through the progress callback and the durable phase marker, record the run completion and the finalising generation through the fenced wrapper, and return the recorded finalisation result unchanged when the run's idempotency key is already finalised
    - Implement dry-run semantics: run-scoped evidence written, no memory-content mutation, and the aborted-run failure state carrying phase and error detail with no certificate
    - _Requirements: 15.2, 15.3, 15.4, 18.9, 18.10, 18.11, 19.3, 32.7, 42.13, 44.5, 44.10, 44.12_

  - [x]* 16.11 Write property test for erasure completeness
    - **Property 1: Erasure completeness**
    - **Validates: Requirements 16.2, 16.3, 16.4, 16.5, 16.6, 16.7, 18.1, 18.4, 10.9**
    - Create `tests/property/test_p01_erasure_completeness.py` with the `memory_graphs()` generator and deterministic stub vectors, asserting no current Attribution_Version for the erased Client remains, every swept candidate carries exactly one Disposition with a known selection reason, and pending-embedding Artifacts are selected like any other

  - [x]* 16.12 Write property test for erasure preservation
    - **Property 2: Erasure preservation**
    - **Validates: Requirements 18.2, 18.4, 18.8**
    - Create `tests/property/test_p02_erasure_preservation.py` reusing `memory_graphs()` and asserting unchanged content digests for Artifacts unbound to the erased Client and a retained Disposition with a non-empty reason for every untouched candidate

  - [x]* 16.13 Write property test for surgical redaction preserving other clients' bindings
    - **Property 3: Surgical redaction preserves other clients' bindings**
    - **Validates: Requirements 18.2, 18.3, 18.4, 18.5, 18.6**
    - Create `tests/property/test_p03_surgical_redaction.py` with the `blended_artifacts()` generator of per-Client labelled segments and a stub rewriter, asserting the row survives, the current binding set equals the original minus the erased Client, both digests are recorded, and the pre-redaction body appears in no row of any table

  - [x]* 16.14 Write property test for dry-run and residue-verb purity
    - **Property 19: Dry-run and residue-verb purity**
    - **Validates: Requirements 17.9, 18.11**
    - Create `tests/property/test_p19_dry_run_purity.py` comparing a digest computed over every memory-content table before and after both a dry-run run and a residue invocation

  - [x]* 16.15 Write property test for erasure idempotence
    - **Property 20: Erasure idempotence**
    - **Validates: Requirements 16.2, 16.3, 16.4, 16.5, 16.6, 18.1**
    - Create `tests/property/test_p20_erasure_idempotence.py` asserting a second run for the same Client changes no memory-content row and produces an empty touched-Artifact list

  - [x]* 16.16 Write the concurrency test for writes against an in-flight erasure
    - Create `tests/concurrency/test_write_during_erasure.py` asserting that an Artifact carrying a binding for the erased Client written concurrently with a running Erasure_Run either aborts with a serialization error or appears in that run's Dispositions, and that the guard read refuses the write with the named domain error
    - _Requirements: 15.3, 36.4_

  - [x]* 16.17 Write the erasure fail-closed and sweep performance tests
    - Create `tests/integration/test_erasure_fail_closed.py` with one fault-injection case per fail-closed path: adjudication unavailable, rewrite unavailable, rewrite answering badly, the self-managed backup path unavailable falling back to a referenced Managed_Backup, both backup paths failing and aborting before mutation, a lease lost mid-run refusing the next evidence write and aborting the run, and serialization retries exhausted leaving a resumable aborted run
    - Create `tests/perf/test_sweep_scale.py` asserting the explicit sweep of 100000 Artifacts completes within 60 seconds
    - _Requirements: 15.5, 16.8, 17.8, 18.7, 19.3, 19.6, 32.1, 44.8_

- [x] 17. Implement the Sensitivity_Analyzer
  - [x] 17.1 Implement Threshold_Grid evaluation over one retained candidate set
    - Write `src/molt/erase/sensitivity.py` with `analyse` and `default_grid`, running one residue search per query Artifact at the widest review threshold in the grid and retaining every candidate with its cosine distance, then evaluating every grid pair by counting against that retained set rather than by re-searching
    - Report per pair the auto-inclusion threshold, the review threshold, the candidate count, the count that pair would include without adjudication, and the count that pair would refer for adjudication, and where the ground-truth mapping is available the count of planted cross-client fragments recovered
    - Open no write transaction and connect with the read-only role, so purity is a privilege fact rather than a discipline, and call the configured Text_Provider for no candidate, computing the referred count from the distance and the band boundaries
    - Report a pair whose auto-inclusion threshold exceeds its review threshold as inapplicable with that reason rather than skipping it, so the grid stays rectangular, and default to the 25-pair grid crossing the five auto-inclusion values with the five review values
    - _Requirements: 48.1, 48.2, 48.3, 48.4, 48.5, 48.6, 48.7, 48.8_

  - [x]* 17.2 Write property test for threshold monotonicity and analysis purity
    - **Property 35: Threshold monotonicity and analysis purity**
    - **Validates: Requirements 48.2, 48.3, 48.5, 48.6, 48.8**
    - Create `tests/property/test_p35_threshold_monotonicity.py` with the `threshold_grids()` generator crossing `contaminated_corpora()` with grids of 1 to 36 pairs whose thresholds are drawn from the unit interval, including pairs in both orders so the inapplicable branch is exercised, plus a table-digest helper hashing every memory-content table before and after, asserting a candidate count no lower when either threshold is raised, inapplicable pairs reported rather than evaluated, no Text_Provider call for any candidate, and byte-identical table digests across the analysis

  - [x]* 17.3 Write the sensitivity analysis performance test
    - Create `tests/perf/test_sensitivity_scale.py` asserting a 25-pair Threshold_Grid analysis over at least 100000 embeddings completes within 120 seconds
    - _Requirements: 48.11_

- [x] 18. Implement certificate generation and independent verification
  - [x] 18.1 Implement certificate assembly, the derived counts, and the corroboration block
    - Write `src/molt/attest/builder.py` assembling the payload from stored evidence only: request identity and justification, Client identity, run row with thresholds and unembedded count, the backup block carrying the path value and the taken and referenced flags, counts with the derivation method, dispositions with both digests and both binding slug arrays, the lineage subgraph edge list, residue candidates with distances and adjudication evidence, session chain tips, the verification-query set drawn from the fixed template list targeting tables outside the working tier only, the cluster audit log window, and the caveat statements
    - Add the ownership, attribution, and checkpoint fields: the Fencing_Generation of the owner that finalised the run, the earliest Attribution_Version validity start and detection method per touched Artifact read before any disposition runs, and the identifier, window bounds, and root digest of the most recent Ledger_Checkpoint whose window end precedes the before timestamp
    - Make the Ledger-plus-Dispositions derivation the primary count mechanism on every certificate, and attempt the historical read only when both timestamps fall inside the measured 4500 second horizon at assembly time, recording the outcome in the corroboration block as attempted, within-horizon, and agreement rather than replacing the derived figures
    - Derive every field from tables outside the working tier
    - _Requirements: 20.2, 20.3, 20.4, 20.6, 20.7, 21.1, 21.2, 21.3, 21.4, 21.5, 21.6, 21.7, 21.8, 21.9, 21.10, 42.10, 42.11, 43.7, 44.11, 45.11_

  - [x] 18.2 Implement signing and Object Lock storage
    - Extend `src/molt/attest/builder.py` to canonicalise, digest with SHA-256, sign the digest with the asymmetric KMS key, attach signature, key identifier, and algorithm to the envelope, persist payload, digest, signature, key identifier, algorithm, and finalising generation in the certificate table through the fenced wrapper before attempting storage, and write the object under the per-Client certificate prefix
    - Apply Object Lock in GOVERNANCE mode with the configured short retention interval, default one day, so teardown can release retention without manual intervention
    - Record the object key and version identifier on success; on failure set the storage status to failed with detail and report it; on KMS unavailability abort certificate creation, retain the run record, and report the signing failure
    - _Requirements: 21.11, 21.12, 21.13, 21.14, 21.15, 21.16, 30.8, 32.4, 32.5, 44.7_

  - [x]* 18.3 Write property test for certificate canonical round trip and schema completeness
    - **Property 11: Certificate canonical round trip and schema completeness**
    - **Validates: Requirements 21.1, 21.2, 21.3, 21.4, 21.5, 21.6, 21.8, 21.9, 21.10, 21.11, 21.12, 20.2, 20.3, 20.4**
    - Create `tests/property/test_p11_certificate_round_trip.py` with the `certificate_payloads()` generator plus key-order and array-order shufflers, asserting byte-identical canonical output, an equivalent parse, every contract key present including the ownership, attribution, and checkpoint fields, each collection agreeing with the stored evidence it derives from, and the derived counts equalling the counts computed independently from the generating memory graph

  - [x] 18.4 Implement the Certificate_Verifier
    - Write `src/molt/attest/verifier.py` loading an envelope from a local path or an object key, recomputing the canonical digest and verifying the signature locally against the KMS public key, validating each embedded query against the fixed template set and executing it with bound parameters under the reader role against tables outside the working tier, verifying every named Session's chain and its terminal digest, verifying the Ledger_Checkpoint the certificate names, and checking disposition consistency with two set-based queries over a bound array
    - Confirm the counts through the derived mechanism as the primary path and record the mechanism used; attempt the historical corroboration only when both timestamps lie inside the horizon read from the capability record, and record an unattempted corroboration as a note rather than a failed check
    - Report signature invalidity, incomplete erasure with the returned identifiers, chain mismatch with the first mismatching sequence number, checkpoint disagreement with its accounting, and the machine-readable overall outcome with the failed-check list
    - _Requirements: 20.8, 22.1, 22.2, 22.3, 22.4, 22.5, 22.6, 22.7, 22.8, 22.9, 42.11, 45.12_

  - [x]* 18.5 Write property test for signature verification detecting any alteration
    - **Property 12: Signature verification detects any alteration**
    - **Validates: Requirements 22.2, 22.3**
    - Create `tests/property/test_p12_signature_alteration.py` with the `signed_certificates()` generator using a local test key plus a byte-index and replacement-byte selector

  - [x]* 18.6 Write property test for working memory disposability
    - **Property 37: Working memory disposability**
    - **Validates: Requirements 42.10, 42.11, 42.12, 42.13**
    - Create `tests/property/test_p37_working_disposability.py` with the `graphs_with_working_state()` generator crossing `memory_graphs()` with 0 to 50 Working_Memory rows across the same Clients with arbitrary values and expiry timestamps, plus a presence selector running the same erasure with the rows present and absent so certificate fields and Verification_Query results are compared pairwise, asserting identity across both runs, that no Lineage_Edge, Client_Binding, Disposition record, or Ledger_Checkpoint references a working row, and that the run removes every working row for the Client while recording one aggregate count and no per-row Disposition

  - [x]* 18.7 Write the signature-invalidation security test
    - Create `tests/security/test_signature_invalidation.py` asserting the verifier reports signature invalidity and exits non-zero when any single byte of a signed certificate payload is altered
    - _Requirements: 22.3, 36.13_

  - [x]* 18.8 Write the KMS and S3 service integration tests
    - Create `tests/integration/services/test_kms_s3.py` covering digest signing, public-key retrieval, versioned GOVERNANCE-mode Object Lock storage, version retrieval, and denial of unencrypted writes. One module rather than two, because every case shares one credential gate and one resolved policy, and a signature written to an object is one path rather than two
    - Assert the storage-failure path retaining the certificate in the cluster in `tests/integration/test_certificate_assembly.py` instead, because that clause needs a reachable cluster and no cloud credential at all: putting it behind the service gate would leave the one governance property here that must never regress unexercised in every credential-free run
    - _Requirements: 21.13, 21.15, 30.8, 30.9, 36.15_

  - [x]* 18.9 Write the verification performance test
    - Create `tests/perf/test_verification_latency.py` asserting verification of a certificate covering 1000 Artifacts completes within 30 seconds
    - _Requirements: 22.11_

- [x] 19. Implement the Checkpoint_Signer
  - [x] 19.1 Implement checkpoint computation, the root digest, and signed storage
    - Write `src/molt/attest/checkpoint.py` with `compute` gathering the terminal Hash_Chain digest and terminal sequence of every Session holding at least one Event inside the window, `root_digest` computing SHA-256 over the covered Session identifiers and terminal digests ordered by Session identifier with fixed separators between fields and between records so no concatenation ambiguity exists, `sign_and_store`, and `latest_before`
    - Sign the root digest with the same asymmetric KMS key and the same digest-signing call shape the Certificate_Builder uses, so one signing key and one signing path exist, and store the window bounds, covered Session count, root digest, signature, key identifier, and algorithm alongside one per-Session row carrying the digest recorded at checkpoint time
    - Run on the configured interval, default 3600 seconds, from the scheduled invocation of the console function, which is the only principal holding the signing permission, and emit `checkpoint.computed`
    - _Requirements: 45.1, 45.2, 45.3, 45.4, 45.5_

  - [x] 19.2 Implement checkpoint verification with accounted and unaccounted disagreement
    - Implement `verify` recomputing the root digest from live Ledger rows for the window, retrieving the public key from KMS, verifying the stored signature against the stored digest, and reporting agreement or disagreement
    - On disagreement, name every covered Session whose terminal digest now differs from the digest recorded at checkpoint time, and for each partition the change: look up the Disposition rows naming that Session's Events and report the identifier of every Erasure_Run whose recorded Dispositions account for the deletions, so a governed erasure is explained rather than flagged and an unaccounted change is the finding
    - Raise `CheckpointDisagreement` carrying the changed Sessions and the accounting runs, emit `checkpoint.verification_disagreements` with the explained dimension, and wire the check into the attest verify path so verifying a certificate also verifies the checkpoint that certificate names
    - _Requirements: 45.6, 45.7, 45.8, 45.12, 46.4_

  - [x]* 19.3 Write property test for checkpoint verifiability
    - **Property 33: Checkpoint verifiability**
    - **Validates: Requirements 45.2, 45.3, 45.6, 45.7, 45.8**
    - Create `tests/property/test_p33_checkpoint_verifiability.py` with the `checkpoint_states()` generator widening `event_sequences()` to 1 to 50 Sessions of 1 to 200 Events each with one computed checkpoint over a window covering all of them, crossed with a change selector over a single-row field mutation, a consistent whole-Session rewrite that leaves the chain self-consistent, and a deletion performed through a real Erasure_Run, so both the accounted and the unaccounted branches are exercised, asserting agreement before the change and disagreement after it naming exactly the changed Sessions with run accounting present for the erasure branch and absent for the mutation branch

  - [x]* 19.4 Write the checkpoint integration and security tests
    - Create `tests/integration/test_checkpoint_verification.py` asserting computation and verification against live rows, that the root digest is a function of content alone by recomputing it from rows read in a different order, and that the certificate's most-recent-before lookup returns the expected checkpoint
    - Create `tests/security/test_checkpoint_beyond_admin.py` asserting a consistently rewritten Session passes per-Session chain verification and fails checkpoint verification, which is the coverage a signature produced outside the cluster adds
    - _Requirements: 45.6, 45.7, 45.14, 36.2_

- [x] 20. Checkpoint - erasure, ownership, and evidence verified
  - Ensure all tests pass, ask the user if questions arise.

- [x] 21. Implement the Policy_Watcher
  - [x] 21.1 Implement the rule set and the pure evaluation function
    - Write `src/molt/policy/rules.py` loading rules from the configured path or the built-in set, and `src/molt/policy/evaluate.py` as a pure function from one mutation plus the rule set to outcomes across the five match kinds, with the sensitive path pattern set covering credential files, key material directories, and environment files, and the fixed severity order halt, require approval, warn, allow
    - _Requirements: 23.4, 23.5, 23.11_

  - [x] 21.2 Implement changefeed consumption as the primary path with the polling fallback
    - Write `src/molt/policy/watcher.py` opening the sinkless `EXPERIMENTAL CHANGEFEED FOR` statement over the Ledger and Derived_Artifact tables with a resolved interval on a dedicated streaming connection as the primary consumption mechanism, resuming from the persisted watermark, recording the changefeed availability in the capability record, and reporting changefeed mode on its health route
    - Implement the fallback retained for tiers that reject the statement: poll by recorded timestamp and identifier from the watermark at the configured interval, emit `watcher.degraded_to_polling`, persist the polling mode, and expose the liveness route reporting mode and last consumed mutation timestamp
    - _Requirements: 23.1, 23.2, 23.3, 23.12, 32.3_

  - [x] 21.3 Implement the kill switch and the approval queue
    - Write `src/molt/policy/apply.py` marking a Session halted with reason and rule inside the 10 second bound, writing one policy match row per applied outcome, inserting one approval queue entry per require-approval match, and recording the resolving principal, decision, and resolution timestamp on resolution
    - Rely on the uniqueness constraints so redelivered mutations after a restart produce no duplicate halts or approvals
    - _Requirements: 23.6, 23.8, 23.10_

  - [x]* 21.4 Write property test for policy evaluation confluence
    - **Property 25: Policy evaluation confluence**
    - **Validates: Requirements 23.3, 23.4, 23.5**
    - Create `tests/property/test_p25_policy_confluence.py` with the `mutation_streams_and_rules()` generator plus a permutation selector over independent mutations, asserting the triggered action set is order-independent and identical under changefeed and polling consumption

  - [x]* 21.5 Write the watcher integration tests
    - Create `tests/integration/test_changefeed_consumption.py` asserting mutations are consumed on the primary path, the capability record reports the changefeed available, the watermark advances, the health route reports changefeed mode, and a restart replays only the unresolved tail
    - Create `tests/integration/test_polling_fallback.py` injecting a changefeed rejection and asserting the polling mode is entered, the metric is emitted, the mode is persisted, and the halt bound still holds
    - _Requirements: 23.1, 23.2, 23.3, 23.6, 32.3, 36.2_

- [x] 22. Implement the Retention_Manager
  - [x] 22.1 Implement expiry computation, TTL configuration across every tier, and the retention report
    - Write `src/molt/retention/__init__.py` with `expiry_for` returning the write timestamp plus the Client Jurisdiction interval, applied at every Artifact write, the TTL application helper used by migrations 006 and 011, and `report` returning per Client the Jurisdiction, the interval, the count expiring within 7 days, and the count already expired
    - Apply the fixed 3600 second interval to the working tier rather than the Jurisdiction interval, because working state's lifetime is a property of the tier rather than of the Client's retention regime, and configure no TTL on the checkpoint tables so a checkpoint outlives the rows it commits to
    - Depend on no scheduled process outside the cluster to delete expired rows
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 42.9, 42.15, 45.10_

  - [x]* 22.2 Write property test for retention expiry monotonicity
    - **Property 21: Retention expiry monotonicity**
    - **Validates: Requirements 14.3, 14.4**
    - Create `tests/property/test_p21_retention_expiry.py` with the `artifacts_across_jurisdictions()` generator over intervals from one hour to ten years and write timestamps across offsets

  - [x]* 22.3 Write the Row-Level TTL integration test
    - Create `tests/integration/test_row_level_ttl.py` asserting expired rows are removed by the cluster with no process outside it on the content tables and on the working table, that the checkpoint tables have no TTL configured and retain rows past every interval, and that the retention report counts agree with the stored rows
    - _Requirements: 14.1, 14.5, 14.6, 42.9, 45.10, 36.2_

- [x] 23. Implement the Molt_MCP_Server
  - [x] 23.1 Implement the tool registry and the four read-only tools
    - Write `src/molt/mcpserver/__init__.py` and `tools.py` with the module-level tool registry as an immutable tuple carrying the recall tool, the two lineage tools, and the residue candidate tool, each declaring its argument schema, its return shape, and a read-only effect, and with dispatch admitting only names present in that tuple so no mutation tool exists or can be reached
    - Back the recall tool with the Recall_Engine, the two lineage tools with the ancestor and descendant queries, and the residue tool with the Residue_Detector's read-only path
    - Resolve the permitted Client set at startup from configuration and never from a tool argument, declare no client-set parameter in any tool schema, apply the tenancy filter inside SQL over unsuperseded Attribution_Versions exactly as the Recall_Engine applies it, and bound every result with the configured maximum result count of 50 applied as the SQL limit rather than as a post-filter
    - Connect with the reader role only
    - _Requirements: 40.1, 40.2, 40.3, 40.5, 40.6, 40.7, 40.8, 40.10, 43.6_

  - [x] 23.2 Implement the stdio and HTTP transports, invocation recording, and the health route
    - Write `src/molt/mcpserver/transport.py` with the stdio transport for a locally spawned server and the HTTP transport for the Fargate service, both speaking the same JSON-RPC framing and both dispatching through the registry
    - Record every invocation as an Event naming the tool, the redacted arguments, and the returned result count, written through the Collector rather than directly, so the server needs no write privilege and recording obeys the capture redaction and signed ingress path
    - Emit the tool invocation metric with its tool dimension, expose the health route on the HTTP transport reporting status, database reachability, the exposed tool names, the permitted Client count, and no memory content, and return an empty result with an error note when the cluster is lost while keeping the transport session open
    - _Requirements: 31.5, 32.7, 40.4, 40.9, 47.10_

  - [x]* 23.3 Write property test for MCP server read-only tenancy
    - **Property 28: MCP server read-only tenancy**
    - **Validates: Requirements 40.6, 40.7, 40.8, 40.9, 40.10**
    - Create `tests/property/test_p28_mcp_read_only_tenancy.py` with the `mcp_invocations()` generator crossing `corpora_with_permissions()` with 1 to 20 invocations across every exposed tool, arguments including well-formed identifiers, absent identifiers, out-of-scope identifiers, and extra keys attempting to name a client set, and requested result counts above and below the maximum, asserting no row of any memory-content table changes, every returned Artifact carries a binding inside the configured permitted set, every result length is within the maximum, and each invocation produces exactly one recording Event with no unredacted argument value

  - [x]* 23.4 Write the MCP server suite
    - Create `tests/mcp/test_tool_registry.py` enumerating the registry and asserting the four expected tool names, their schemas, that every entry declares a read-only effect, and that no mutation tool exists
    - Create `tests/mcp/test_transports.py` completing one handshake over the stdio transport and one over the HTTP transport
    - Create `tests/mcp/test_tenancy_source.py` asserting the permitted Client set is read from configuration and that a tool argument attempting to name one is ignored
    - _Requirements: 36.18, 40.1, 40.2, 40.3, 40.4, 40.6, 40.7_

- [x] 24. Ship the executable Agent_Skills
  - [x] 24.1 Write the three skill definitions and the skills index
    - Write `skills/verify-certificate/` as an Agent_Skill in the open Agent Skills format that verifies an Erasure_Certificate against a live cluster and reports the outcome and any failed checks, with an entry point invoking the attest verify path and the lineage tools under the reader role only
    - Write `skills/residue-sweep/` running a residue sweep for a named Client and reporting candidates with distances and decisions through the residue tool, which performs no mutation
    - Write `skills/retention-audit/` auditing retention status per Client and reporting Jurisdiction, interval, and expiring and expired counts through the Retention_Manager report path
    - Declare the inputs, the outputs, and the behavior in each definition's own format fields, restrict every declared operation to the read-only set, and add `skills/README.md` naming the open format used and how an MCP-compatible client loads these without modification
    - _Requirements: 39.1, 39.2, 39.3, 39.4, 39.5, 39.6, 39.7_

  - [x]* 24.2 Write the per-skill loading tests
    - Create `tests/skills/test_verify_certificate_skill.py`, `tests/skills/test_residue_sweep_skill.py`, and `tests/skills/test_retention_audit_skill.py`, each asserting the definition parses, the declared inputs, outputs, and behavior fields are present, every declared operation falls inside the read-only set, and the declared entry point executes against a seeded local instance
    - _Requirements: 36.17, 39.5, 39.6_

- [x] 25. Implement the Seed_Generator
  - [x] 25.1 Implement seeded clients, sessions, events, derived artifacts, procedure standing, and attribution history
    - Write `src/molt/seed/corpora.py` and `src/molt/seed/generator.py` producing 4 Clients with synthetic domains and content markers, 28 Sessions across 5 Agent_CLI names and 4 machine identifiers, 3 subagent Sessions at depth 2 and one at depth 3, at least 2600 Events with outcomes spanning succeeded, failed, and abandoned, and Summary, Behavioral_Baseline, and Learned_Procedure artifacts derived from real parents with Lineage_Edges per parent
    - Drive each seeded Learned_Procedure from the configured initial confidence through the ordinary Confidence_Tracker path using the outcomes of the Sessions that retrieved it, and drive at least one below the recall floor so the excluded-but-retained state is observable and the sweep's inclusion of it is testable
    - Give a handful of seeded Artifacts a marker detection after an initial scope detection so a genuine supersession exists with a closed validity interval and a Ledger Event, and seed working-tier rows across the seeded Clients so the disposability property has state to compare
    - Drive all randomness from one seeded generator in a fixed traversal order, embed every embeddable Artifact through the normal Embedder path so vectors come from the configured provider, and implement the reset flag truncating only seeded Clients and refusing to run when a non-seeded Client exists unless confirmed
    - _Requirements: 28.1, 28.2, 28.3, 28.4, 28.7, 28.9, 42.7, 43.3, 49.2, 49.9, 49.11_

  - [x] 25.2 Implement contamination planting and the separated ground truth
    - Write `src/molt/seed/contaminate.py` planting at least 8 fragments: draw distinct owner and host Clients, generate a 15 to 60 line fragment from the owner's domain vocabulary, strip the owner's slug, display name, repository name, content markers, directory names, and every path-like string, assert the absence of each stripped token and raise if any survives, wrap the fragment in a host action inside a Session scoped to the host Client, and write it through the normal path so only a scope binding for the host is detected
    - Write the ground-truth mapping outside the database in the untracked seed file, then re-read each planted Event and fail if any owner token or owner binding is present
    - _Requirements: 28.5, 28.6, 28.7_

  - [x]* 25.3 Write the seed determinism and contamination assertion tests
    - Create `tests/integration/test_seed_determinism.py` generating twice with the same seed and asserting content equality after replacing identifiers and timestamps with positional placeholders
    - Create `tests/integration/test_seed_determinism.py` asserting every planted fragment carries no owner Client token, no owner Client binding, and no path revealing the owner, and that the ground-truth file lives outside the cluster and is referenced by no row. It sits in the instance-backed suite rather than the credential-free security suite because every assertion reads planted rows from a live cluster, and a cluster-dependent case under `tests/security/` would break that suite's credential-free contract
    - _Requirements: 28.5, 28.6, 28.9_

  - [x]* 25.4 Write the residue ground-truth recovery test
    - Create `tests/integration/test_residue_ground_truth.py` asserting the Residue_Detector recovers every planted fragment recorded in the ground-truth mapping, using the mapping only to check the answer and never to seed the query. It sits in the instance-backed suite for the same reason the contamination assertions do: the recovery is a vector search against a live corpus
    - _Requirements: 36.14, 48.4_

- [x] 26. Implement the Web_Console
  - [x] 26.1 Implement the application skeleton, the Lambda adapter, authentication, the health route, and the specification route
    - Write `src/molt/console/app.py`, `auth.py`, `deps.py`, and `lambda_adapter.py` with the Starlette application, a small ASGI-to-Lambda adapter so the same application object is served locally by the serve verb and by the deployed function, the operator credential verified by constant-time comparison against a hash read from Parameter_Store, a signed HttpOnly, Secure, SameSite=Strict cookie with absolute expiry, per-session CSRF tokens on mutation routes, and the unauthenticated health route reporting status, database reachability, the capability record summary, the demo flag, and no memory content
    - Add the unauthenticated specification route serving the tracked Interface_Specification document from the documentation directory verbatim with the appropriate content type, reading no table and returning no memory content
    - Write `web/templates/base.html` and `web/static/molt.css` with native focusable controls, visible focus outlines, and contrast that meets the 4.5:1 ratio for text
    - _Requirements: 25.1, 25.9, 25.10, 25.11, 25.14, 30.2, 30.5, 31.5, 34.2, 51.4_

  - [x] 26.2 Implement the fleet, session, and lineage views
    - Write `src/molt/console/routes/fleet.py` and `routes/lineage.py` with the fleet overview, the per-Session Event stream including chain verification status, the filterable lineage view, and the per-Artifact ancestor and descendant subgraph
    - Write `web/templates/fleet.html`, `session.html`, and `lineage.html` rendering inline SVG with per-node title elements, layer-ordered focusable nodes, accessible names stating kind, bindings, and creation time, a text label and shape per kind so colour is never the only channel, and an equivalent edge-list table below the diagram
    - _Requirements: 8.6, 11.5, 11.6, 25.2, 25.3, 25.11_

  - [x] 26.3 Implement the residue view and the erasure console with durable phase streaming
    - Write `src/molt/console/routes/residue.py` and `routes/erasure.py` with the residue view, the erasure console, the run-start route recording the run and returning the run identifier immediately, and the stream route sourced from the durable phase marker and the disposition and residue candidate rows rather than from process memory, so a late or reconnecting client receives current state under a request-scoped function host, terminating with an explicit terminal event carrying the outcome
    - Write `web/templates/residue.html` and `erase.html` with a native select for the Client, native number inputs for thresholds, a form button to start, and a polite live region that does not move focus
    - _Requirements: 25.4, 25.5, 25.11_

  - [x] 26.4 Implement the run detail, redaction comparison, certificate, retention, and approval views
    - Write `src/molt/console/routes/runs.py` (run detail and redaction comparison), `routes/certificates.py` (certificate display and the live verification trigger), `routes/retention.py` (the retention view), and `routes/approvals.py` (the approval queue list and its resolution route). Delivered as four modules rather than the two the wording names: each claims its own routes from the route table, and the certificate module is the only one of the four that reads through the read-only handle for a reason of its own — the Certificate_Verifier refuses a connection whose role can write, so the verification trigger cannot run on the eraser handle the console function holds
    - Write the matching templates; the redaction view shows the post-redaction body, both digests, the binding sets before and after, the stored structural diff summary, and an explicit statement that the original text was not retained
    - Show the certificate's ownership generation, first-attribution field, and named checkpoint alongside the verification outcome, so the new evidence fields are observable rather than only stored
    - _Requirements: 18.8, 23.10, 25.6, 25.7, 25.8, 43.7, 44.11, 45.11_

  - [x] 26.5 Implement the sensitivity grid and procedure standing views
    - Write `src/molt/console/routes/sensitivity.py` rendering the Sensitivity_Analyzer report as a table whose rows are auto-inclusion threshold values and whose columns are review threshold values, each applicable cell showing the candidate count, the auto-included count, the adjudication-referred count, and the recovered planted-fragment count where ground truth is available, stacked with text labels rather than distinguished by colour alone, and each inapplicable cell rendering the word inapplicable with its reason so the grid stays rectangular
    - Write `src/molt/console/routes/procedures.py` listing each Learned_Procedure with its current Procedure_Confidence, its retrieval count, and its outcome counts per classification, each row expanding to the ordered change history naming prior value, new value, and triggering outcome, and showing procedures below the recall floor marked as excluded from recall and retained in storage
    - Write `web/templates/sensitivity.html` and `procedures.html`, both read-only in every mode including demonstration mode because the analyser opens no write transaction and connects with the reader role
    - _Requirements: 48.5, 48.9, 48.10, 49.15, 49.16_

  - [x] 26.6 Implement read-only demonstration mode
    - Write `src/molt/console/demo.py` as middleware rejecting every route classified as a mutation with 403 by route name before the handler runs, establishing an anonymous read-only principal restricted to the seeded Clients, rendering a mode banner, and rendering blocked controls as disabled with an accessible explanation rather than hiding them
    - Replay a completed seeded run through the same streaming view so phase streaming, the redaction comparison, and certificate verification remain observable without a mutation route
    - _Requirements: 25.5, 25.6, 25.7, 25.12_

  - [x]* 26.7 Write property test for route-table authentication, demonstration containment, and specification coverage
    - **Property 38: Route-table authentication and demonstration-mode containment**
    - **Validates: Requirements 25.9, 25.10, 25.12, 30.5, 51.4**
    - Create `tests/property/test_p38_route_auth.py` with the `route_requests()` generator enumerating the application's own route table from the ASGI object the deployed function serves, crossed with authenticated, unauthenticated, and demonstration contexts, and crossed with the parsed Interface_Specification, asserting 401 or 403 outside the public allowlist, no memory-content key in the health body or the specification body, every declared route present in the specification, and 403 on every mutation route in demonstration mode

  - [x]* 26.8 Write the console template and accessibility tests
    - Create `tests/unit/test_console_templates.py` asserting every interactive control in the rendered erasure console carries a programmatically determinable name, that no control is a bare container with a handler, that the streaming region is a live region, and that the sensitivity grid renders inapplicable cells with their reason and the procedures view marks below-floor procedures as retained
    - Assert the Memory_Tier view renders one row per tier each carrying a text label, that the `working` tier row carries its expired-count cell and its next-sweep cell, and that the tier mapping module and the rendered view name the same tier set
    - _Requirements: 25.11, 25.15, 42.20, 48.8, 49.16_

  - [x] 26.9 Implement the Memory_Tier view
    - Write `src/molt/console/routes/tiers.py` reading the tier mapping module and issuing one `COUNT(*)` statement per tier inside one read-only transaction, opening no write transaction, with every count derived at request time rather than from a cached or precomputed value
    - Add for the `working` tier the count of resident rows whose expiry timestamp precedes the cluster's current timestamp, and the interval remaining until the next Row-Level TTL job run computed from the TTL job cron storage parameter read back from the table's own configuration rather than from a hardcoded value or a configuration key
    - Write `web/templates/tiers.html` rendering one row per tier with a text label per tier and no colour-only encoding, mutability and capability as prose cells, and the working tier's expired-resident count and next-sweep interval in their own labelled cells
    - The view is available in read-only demonstration mode because it opens no write transaction and connects with the reader role, so the mutation denylist has nothing to block on it. Delivered through `Console.read_only_store()`, which is the reader connection wherever the deployment provisions one — the delivered console does — and the primary connection where it provisions none, so a single-connection local run keeps the view rather than losing it to a privilege the view never depended on. The read-only transaction stands either way, and `tests/unit/test_console_read_only_handles.py` refuses any console module that writes nothing and still reaches for the wider handle
    - _Requirements: 25.15, 42.16, 42.17, 42.18, 42.19, 42.20_

- [x] 27. Implement the command-line interface
  - [x] 27.1 Implement the argparse tree, output contract, and exit codes
    - Write `src/molt/cli/__init__.py` and `main.py` with nested subparsers for the erase, residue, sensitivity, contend, attest verify, recall, watch, serve, mcp, seed, migrate, verify-chain, and retention verbs, the global JSON, config, client-set, log-level, and confirmation flags, and exit codes 0, 1 for operational failure, 2 for usage or configuration error, and 3 for a verification outcome of failed
    - Route all output through one formatter that redacts any value whose key matches the secret-name set and diverts human-readable narration to standard error under the JSON flag
    - _Requirements: 26.1, 26.2, 26.3, 26.4, 26.5, 26.6, 26.7_

  - [x] 27.2 Wire the verb modules to their components
    - Write one module per verb under `src/molt/cli/verbs/` calling the Erasure_Engine, the Residue_Detector against a synthetic dry-run row that mutates no memory content, the Sensitivity_Analyzer under the reader role printing the grid table and its JSON form, the lease contention demonstration, the Certificate_Verifier including the checkpoint check and the standalone checkpoint argument, the Recall_Engine, the Policy_Watcher including the single-batch mode, the Web_Console, the Molt_MCP_Server, the Seed_Generator, the migration runner, the chain verifier, and the retention report, with the argument surfaces of the design verb table
    - Implement the contend verb printing the winning owner identifier, the Fencing_Generation recorded at each takeover, and the refusal outcome of the revived worker's disposition write, exiting non-zero if exactly one worker does not win, if takeover succeeds before expiry, or if the stale write is not refused
    - Resolve the MCP server's permitted Client set from the verb arguments and the configuration file at startup and never from a tool argument
    - _Requirements: 8.6, 8.7, 13.9, 14.5, 17.9, 18.10, 18.11, 19.4, 22.10, 23.13, 25.13, 27.2, 28.8, 40.4, 40.7, 40.10, 40.11, 44.13, 44.14, 45.12, 48.9_

  - [x]* 27.3 Write CLI unit tests
    - Create `tests/unit/test_cli_parsing.py` asserting the verb and flag surface including the sensitivity and contend verbs and the two-word attest verify form, the distinct exit codes, the missing-configuration message naming the key, the JSON flag emitting one object on standard output, and no secret value in any stream
    - _Requirements: 26.2, 26.3, 26.4, 26.6, 26.7, 36.1_

- [x] 28. Integrate Telemetry across the components
  - [x] 28.1 Implement CloudWatch delivery and the metric inventory
    - Extend `src/molt/telemetry/` with buffered batched metric delivery to CloudWatch under the configured namespace, the full metric inventory from the design table including the adjudication cache-token metrics, the tool invocation metric, the stale generation refusal metric, the lease takeover metric, the working rows deleted metric, the signature rejection metric, the attribution supersession metric, the checkpoint computed and disagreement metrics, the confidence change and recall floor metrics, and the cardinality overflow counter
    - Apply the cardinality bound before every emission with overflow diverted to structured log records, keep the standard-error fallback that continues on a CloudWatch failure, and establish the Erasure_Run identifier as the correlation identifier for every record produced during a run
    - Attach the Client slug and Agent_CLI dimensions only to the metrics where the per-Client breakdown earns its place, and emit everything else with the component dimension alone so the bound is not spent on low-value combinations
    - _Requirements: 31.1, 31.2, 31.3, 31.6, 33.13, 33.14, 38.5, 40.9, 42.13, 43.3, 44.6, 44.15, 45.1, 45.7, 47.13, 49.9, 49.12_

  - [x] 28.2 Wire metric and log emission, the health routes, and graceful termination into every component
    - Add the emission calls to the Capture_Hook, Collector, Provider_Selector, Embedder, Recall_Engine, Memory_Store, Lease_Manager, Erasure_Engine, Residue_Detector, Adjudicator, Redaction_Rewriter, Sensitivity_Analyzer, Confidence_Tracker, Checkpoint_Signer, Backup_Manager, Certificate_Verifier, Policy_Watcher, and Molt_MCP_Server at the points the metric table names, and confirm the Collector, Policy_Watcher, Web_Console, and Molt_MCP_Server health routes report component status and database reachability
    - Add graceful termination handling that stops accepting work, lets in-flight transactions settle, closes pool connections, and flushes telemetry
    - _Requirements: 31.1, 31.3, 31.5, 32.7_

  - [x]* 28.3 Write telemetry unit and CloudWatch service integration tests
    - Create `tests/unit/test_telemetry_cardinality.py` asserting content bodies, credential values including model provider credentials and the ingress signing secret, and vectors are dropped and that the four fixed keys are always present
    - Create `tests/integration/services/test_cloudwatch.py` asserting a live batched metric put and the standard-error fallback on failure
    - _Requirements: 31.2, 31.4, 31.6, 36.15, 37.12_

- [x] 29. Implement provisioning scripts and infrastructure definitions
  - [x] 29.1 Write the cluster and role provisioning scripts with the capability probes
    - Write `scripts/provision_cluster.sh` creating the cluster through the ccloud CLI, `scripts/provision_roles.sh` creating one service account per role, storing every credential in Parameter_Store standard tier and printing no credential value, and creating per-Auditor read-only accounts with an expiry interval of at most 30 days along with the per-Auditor schema of Client-filtered views granted select only, including an as-of-attribution view restricted to that Auditor's own Client
    - Add the capability probes the Provisioner owns: read the rangefeed cluster setting and record it, interrogate the control plane through the ccloud CLI for an on-demand backup creation operation and record its absence with the detail that the control plane offers listing and configuration only, and verify every required model identifier is reachable in the deployment region, reporting the unreachable identifier and exiting non-zero
    - Apply every migration in order through the migration runner and record the applied versions, and write `scripts/pull_audit_log.sh` pulling cluster audit logs for a caller-supplied window; make every script idempotent so a second run changes no state, and invoke Python as `python3.12` throughout
    - _Requirements: 23.14, 24.1, 24.2, 24.4, 24.5, 27.1, 27.2, 27.6, 27.7, 27.8, 27.9, 27.11, 30.2, 34.10, 34.11, 43.11_

  - [x] 29.2 Write the foundation infrastructure templates
    - Write `infra/templates/network.yaml` creating a VPC with public subnets only, security groups declaring zero ingress rules, no NAT gateway, and no interface endpoint
    - Write `infra/templates/parameters.yaml` declaring every parameter in the standard tier and carrying no values, including the ingress signing secret parameter, `kms.yaml` with the asymmetric signing key whose policy grants the sign operation only to the console execution role, and `storage.yaml` with Object Lock enabled at bucket creation because it can be enabled on no existing bucket, GOVERNANCE mode with the short configured retention interval, versioning, blocked public access, required encryption, and a bucket policy denying unencrypted writes and every principal outside the named roles
    - Write `infra/params/demo.json` with non-secret parameter values only
    - _Requirements: 21.13, 30.8, 30.9, 33.10, 33.11, 33.12, 34.4, 34.6, 47.2_

  - [x] 29.3 Write the service templates and deployment wrappers
    - Write `infra/templates/collector.yaml` with the Lambda function, function endpoint, the declared reserved concurrency of 10, execution role, and log group; `console.yaml` with the Web_Console as a Lambda function with a function endpoint, execution role, log group, and the scheduled rule invoking the Checkpoint_Signer entry point in the same function at the configured interval; `cdn.yaml` with the CloudFront distribution using its own default certificate and generated hostname and the console function endpoint as its single origin; `watcher.yaml` with the ECS cluster, task definition, and service for the Policy_Watcher in a public subnet with no ingress listener; `mcp.yaml` with the task definition and service for the Molt_MCP_Server in a public subnet with no ingress listener; and `observability.yaml` with log groups, metric filters, alarms, and the metric cardinality guard
    - Create no Application Load Balancer, no target group, no HTTPS listener, no NAT gateway, and no interface endpoint in any template
    - Write `infra/deploy.sh` deploying stacks in order with parameter validation and idempotent behaviour, `infra/teardown.sh` deleting in reverse order and releasing the GOVERNANCE-mode Object Lock retention on each certificate version first so teardown completes without manual intervention, and `infra/README.md` naming what each stack creates and which requirement it satisfies
    - Grant each role only the permissions in the design table, with the signing permission on exactly one role and no wildcard resource outside the namespace-conditioned metric statement
    - _Requirements: 5.12, 23.12, 25.14, 30.4, 30.5, 30.9, 30.11, 31.5, 33.10, 33.11, 34.1, 34.2, 34.5, 34.6, 34.9, 45.1_

  - [x]* 29.4 Write the infrastructure template test suite
    - Create `tests/infra/test_templates.py` carrying all three groups of assertions below. They are one module rather than three because every case parses the same template set once, and a fixture shared across three modules would either parse the templates three times or introduce a shared-state import between them
    - Assert the absences: no load balancer resource, no target group, no HTTPS listener, no NAT gateway, and no interface endpoint resource exists in any template, and no per-secret secret resource exists
    - Assert the shapes: every Fargate security group declares zero ingress rules, the CDN's single origin is the console function endpoint, every parameter resource declares the standard tier, the ingress signing secret is declared as a parameter resource, the Collector function declares the configured reserved concurrency, the scheduled rule targets the console function's checkpoint entry point, the bucket declares Object Lock enabled at creation in GOVERNANCE mode, and the signing permission appears in exactly one role's policy
    - Assert the synthesis: every template synthesises and deployment parameter validation rejects a missing parameter
    - _Requirements: 5.12, 25.14, 30.8, 30.9, 33.10, 33.11, 33.12, 34.6, 34.9, 45.1, 47.2_

  - [x]* 29.5 Write the remaining service integration tests
    - Create `tests/integration/services/test_parameter_store.py` covering standard-tier parameter retrieval and operator credential file retrieval, `tests/integration/services/test_ccloud_backup.py` covering the primary `BACKUP INTO` path, Managed_Backup listing for the fallback, and the audit-log pull, and `tests/integration/services/test_managed_mcp.py` covering Managed MCP Server connectivity under the read-only role
    - _Requirements: 19.1, 19.6, 24.1, 27.8, 27.11, 30.2, 30.12, 36.15_

- [x] 30. Checkpoint - full system wired
  - Ensure all tests pass, ask the user if questions arise.

- [x] 31. Write and enforce the Interface_Specification
  - [x] 31.1 Write the Interface_Specification document
    - Write `docs/interface.json` as a machine-readable OpenAPI document describing every Collector route, every Web_Console route, and every Molt_MCP_Server tool, with the request shape, the response shape, the authentication requirement, and the error responses for each, naming the bearer-only routes, the bearer-plus-signature routes, the unauthenticated health and specification routes, and the 401, 403, 413, and 503 error responses
    - Describe the MCP tools as operations under a dedicated path prefix with their argument and result schemas, so one document covers both surfaces rather than splitting the vocabulary
    - Name the Memory_Tier view route with its response shape, covering the per-tier descriptive columns, the live row count, and the working tier's expired-count and next-sweep figures, and with its authentication requirement
    - _Requirements: 51.1, 51.2, 51.3, 47.11, 47.12, 42.21_

  - [x]* 31.2 Write the Interface_Specification conformance suite
    - Create `tests/spec/test_specification_parses.py` asserting the document parses and that every route the Web_Console route table declares appears in it with a request shape, a response shape, an authentication requirement, and error responses
    - Create `tests/spec/test_tool_coverage.py` asserting every tool the Molt_MCP_Server registry exposes appears in the document
    - Create `tests/spec/test_served_specification.py` asserting the served specification body equals the tracked document and contains no memory content
    - _Requirements: 51.4, 51.10, 51.11_

- [x] 32. Complete the end-to-end, security, and performance coverage
  - [x]* 32.1 Write the end-to-end test
    - Create `tests/e2e/test_full_flow.py` executing seed, capture with a signed ingest request, recall showing a confidence-weighted procedure, a sensitivity analysis, a checkpoint computation, a leased erase, certify, and verify in sequence against a running instance, asserting the certificate verifies, that its counts are confirmed through the derived mechanism so the run does not depend on the garbage-collection horizon, and that its ownership generation, named checkpoint, first-attribution field, and working-rows-deleted count all agree with the database
    - _Requirements: 20.2, 36.12, 42.13, 43.7, 44.11, 45.11, 47.1, 48.9_

  - [x]* 32.2 Write the remaining performance tests
    - Create `tests/perf/test_concurrent_machines.py` asserting concurrent Event writes from at least 20 distinct machine identifiers succeed with no lock shared across unrelated Sessions
    - Create `tests/perf/test_attribution_as_of.py` asserting the as-of-attribution query answers within 1 second for an Artifact carrying at least 100 Attribution_Versions
    - _Requirements: 33.1, 43.10_

  - [x]* 32.3 Write the remaining security and regression tests
    - Create `tests/security/test_route_authentication.py` asserting every network-exposed route other than the named health and specification routes requires authentication
    - Create `tests/security/test_credential_absence.py` scanning every tracked file for credential, connection string, bearer token, private key, ingress signing secret, and model provider credential shapes
    - Create `tests/unit/test_provider_credential_render.py` asserting a loaded provider credential renders as the fixed placeholder in every log record, exception message, error detail, and output stream on the real emission path
    - _Requirements: 30.1, 30.5, 30.12, 37.12, 51.4_

- [x] 33. Write the documentation deliverables
  - [x] 33.1 Write the README and the architecture documentation
    - Write `README.md` describing the problem, the architecture, each CockroachDB tool used and what Molt does with it, each AWS service used and what Molt does with it, and the demonstration URL at the CDN distribution's generated hostname
    - Write `docs/architecture.md` and `docs/architecture.svg` showing capture, the CockroachDB memory core across its six tiers, the erasure engine with its lease ownership, the provider abstraction, the Molt_MCP_Server, and the AWS services
    - _Requirements: 34.7, 34.8, 35.2, 35.5_

  - [x] 33.2 Write the setup guide
    - Write `docs/setup.md` walking through provisioning the cluster, applying both migration generations, deploying the stacks in order, selecting the providers and loading their credentials from files or Parameter_Store, registering the per-tool hooks with the ingress shared secret, seeding data, running an Erasure_Run under a lease, computing a checkpoint, and running the MCP server, ending with the four static checks and the hygiene check as the last steps
    - _Requirements: 27.2, 29.8, 35.3, 50.9_

  - [x] 33.3 Write the per-tool hook specification notes
    - Write `docs/hooks/claude_code.md`, `cursor.md`, `codex.md`, `gemini_cli.md`, and `copilot.md`, each recording the vendor specification location, the hook event names, the exact field names consumed, the context-injection envelope or the advisory text channel where no structured envelope exists, the blocking-decision channel or the documented non-zero-exit convention, and the resulting capability flags
    - _Requirements: 1.9, 13.6, 29.9_

  - [x] 33.4 Write the auditor guide
    - Write `docs/auditor.md` from a template parameterised by the Managed MCP Server endpoint and the service account identifier, with connection instructions for the three named editors, the per-Client view list including the as-of-attribution view, the query-logging statement, and an explicit statement that read-only access is the intended posture for an untrusted third party, carrying no credential value
    - _Requirements: 24.1, 24.3, 24.6, 24.7, 43.11_

  - [x] 33.5 Write the provider, MCP server, skills, and platform-fact documentation
    - Write `docs/providers.md` recording that the abstraction exists so provider availability is a configuration concern rather than an architectural one, naming Bedrock as the default implementation for both roles, naming the delivered selection for each of the Embedder, the Adjudicator, and the Redaction_Rewriter, and recording the chosen deployment region and the verified model identifiers
    - Write `docs/mcp.md` recording the four tools, both transports, the read-only posture, the configuration-sourced permitted Client set, and the default result bound, and `docs/skills.md` recording the three shipped Agent_Skills, the open format used, how a client loads them, and that the schema and query review obligation stands alongside them
    - Record in the platform documentation that the distributed vector index is created on the delivered cluster tier with its reported operator class and that the exact-scan path exists only as a fallback, that sinkless changefeeds are permitted with the rangefeed setting enabled and polling is retained only as a fallback, that the measured garbage-collection horizon is 4500 seconds and the derived count mechanism is therefore primary, that Managed_Backups run on a fixed 24-hour schedule with a 30-day retention interval Molt leaves unaltered so the self-managed path is primary, that Object Lock COMPLIANCE mode is the production posture while the delivered configuration uses GOVERNANCE mode with a short interval, that a Ledger_Checkpoint is tamper evidence rather than tamper proofing and extends coverage beyond a cluster administrator because the signing key lives outside the cluster, that the per-Session chain reaches a certificate only for touched Sessions while checkpoints cover every Session in the window, that the bearer token resists no replay and the Ingress_Signature bounds the replayable window to the configured maximum request age, and that the Sensitivity_Analyzer calibrates thresholds and replays no recorded adjudication decision
    - _Requirements: 10.12, 19.8, 20.7, 21.16, 23.15, 34.12, 37.6, 37.13, 37.14, 39.8, 40.1, 40.4, 40.10, 45.13, 45.14, 45.15, 47.14, 48.12_

  - [x] 33.6 Write the glossary and the Threat_Model
    - Write `docs/glossary.md` defining every domain term, every system component name, and every external service name the documentation uses, including the terms this scope adds: Memory_Tier, Working_Memory, Attribution_Version, Erasure_Lease, Fencing_Generation, Ledger_Checkpoint, Ingress_Signature, Threshold_Grid, Procedure_Confidence, Interface_Specification, and Threat_Model
    - Write `docs/threat-model.md` recording every trust boundary of the delivered configuration and the seven named threats — credential compromise, Ledger tampering, concurrent erasure ownership, ingress replay, tenancy escape through a tool argument, prompt injection into an adjudication prompt, and provider credential leakage — each with the mitigation applied and the requirement specifying it, and recording plainly which threats are accepted in part rather than mitigated in full together with the reason for that acceptance
    - _Requirements: 51.5, 51.6, 51.7, 51.8, 51.9_

  - [x] 33.7 Write the memory tier and structural protection documentation
    - Write `docs/memory-tiers.md` with the Memory_Tier table naming each tier, the tables that tier holds, the mutability of that tier, and the CockroachDB capability it relies on, and recording that the working tier is disposable and that its expiry is enforced by Row-Level TTL rather than by a scheduled process outside the cluster
    - Generate that table from the tier mapping module rather than maintaining it by hand, so the document and the console view state one taxonomy, and record that the taxonomy is observable at runtime at the console tier route
    - Write `docs/protection.md` recording which tables are protected by restricted referential actions and which cascade, with the reason each table falls in its group, and recording that the eraser role's loss of delete privilege on the audit tables is the privilege half of the same protection
    - _Requirements: 42.14, 42.15, 46.5, 46.8_

  - [x] 33.8 Write the typing and hygiene documentation
    - Write `docs/typing.md` recording the exact commands that run the strict type check, the linter check, and the formatter check on a developer machine as the same invocations the workflow uses, and the type-ignore allowlist as a table of file path, exact directive, and reason
    - Write `docs/hygiene.md` recording the pattern classes, the denylist and allowlist rationale, and the reason the denylist file is the only path excluded from its own scan
    - _Requirements: 29.4, 29.8, 50.3, 50.9_

  - [x] 33.9 Write the cost record, reviews record, traceability table, and recording script
    - Write `docs/cost.md` stating a maximum monthly cost for the delivered configuration, with a table naming each service, its estimated monthly consumption, and its estimated monthly cost, plus the measured storage footprint, the measured request-unit consumption, the measured prompt-cache hit ratio and the resulting cost per Erasure_Run, and the note that cluster consumption is covered by introductory credits rather than a perpetual free tier and that Fargate, per-secret secret storage, and asymmetric key storage carry no perpetual free tier
    - Write `docs/reviews.md` recording the schema and query reviews conducted with the Agent Skills material and the changes those reviews produced, `docs/traceability.md` mapping each judging criterion to the requirements that address it, and `docs/demo.md` as a recording script of at most three minutes covering capture on two machines, semantic recall changing an agent decision, residue detection, an Erasure_Run, and certificate verification
    - _Requirements: 27.10, 33.5, 33.6, 33.9, 35.9, 35.10, 38.6_

  - [x]* 33.10 Run the hygiene and glossary coverage checks across the completed tree
    - Extend `tests/security/test_hygiene_check.py` with a case asserting the check exits 0 over the whole repository, so every documentation file, skill definition, migration, and workflow definition written by the tasks above is covered
    - Create `tests/spec/test_glossary_coverage.py` asserting every component name the design and the README use appears in the glossary, so a component added without a definition is a failure rather than a gap a reader discovers
    - _Requirements: 29.4, 29.5, 29.8, 51.5_

- [x] 34. Final checkpoint - full plan verified
  - Ensure all tests pass, the four static checks report clean, the hygiene check exits 0 over the whole tree, and every property test from Property 1 through Property 40 is present and passing; ask the user if questions arise.

## Notes

- Tasks marked `*` are optional test tasks and can be skipped for a faster path; the code they verify is never optional
- Each leaf sub-task carries a requirements trace line citing specific numbered acceptance criteria
- Checkpoints at tasks 3, 9, 14, 20, 30, and 34 give incremental validation points
- Property tests cover all forty design properties, one Hypothesis test per property, each drawing from the generator the design names
- Unit, integration, concurrency, service, skills, MCP, infrastructure, quality, specification, end-to-end, security, and performance suites carry everything the design deliberately excludes from property testing
- The four static checks precede every suite, locally and in the workflow, so a type error or a stray ignore directive fails in seconds
- No task commits, pushes, publishes, or opens a change proposal, and no task reads or copies from the ignored reference directory

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4"] },
    { "id": 2, "tasks": ["1.5", "1.6", "2.1", "2.3", "2.4", "2.8", "2.11"] },
    { "id": 3, "tasks": ["1.7", "1.8", "2.2", "2.5", "2.6", "2.7", "2.9", "2.10", "4.1", "5.1"] },
    { "id": 4, "tasks": ["4.2", "4.3", "4.4", "5.2", "5.3", "5.4", "5.5", "5.6", "5.7"] },
    { "id": 5, "tasks": ["4.5", "5.8", "6.1", "6.2", "6.3", "6.4", "6.5"] },
    { "id": 6, "tasks": ["6.6", "6.7", "6.11", "6.12", "7.1"] },
    { "id": 7, "tasks": ["6.8", "6.9", "6.10", "7.2", "7.5", "7.8", "7.11", "7.12"] },
    { "id": 8, "tasks": ["7.3", "7.4", "7.6", "7.7", "7.9", "7.10", "7.13", "7.17"] },
    { "id": 9, "tasks": ["7.14", "7.15", "7.16", "8.1", "8.5", "8.6"] },
    { "id": 10, "tasks": ["8.2", "8.3", "8.4", "8.7", "10.1", "10.3", "10.5", "10.7", "12.1", "12.3"] },
    { "id": 11, "tasks": ["10.2", "11.1", "12.5", "13.1"] },
    { "id": 12, "tasks": ["10.4", "10.6", "10.8", "10.9", "11.2", "12.2", "12.4", "12.6", "12.7", "12.8", "12.9", "13.2", "13.3", "13.4"] },
    { "id": 13, "tasks": ["11.3", "11.4", "11.5", "11.6", "11.7", "11.8", "15.1"] },
    { "id": 14, "tasks": ["15.2", "16.1", "16.2", "16.3", "16.6", "16.7", "16.8"] },
    { "id": 15, "tasks": ["15.3", "15.4", "15.5", "16.4", "16.5", "16.10", "17.1"] },
    { "id": 16, "tasks": ["16.11", "16.12", "16.13", "16.14", "16.15", "16.16", "16.17", "17.2", "17.3", "18.1"] },
    { "id": 17, "tasks": ["16.9", "18.2", "18.4", "19.1"] },
    { "id": 18, "tasks": ["18.3", "18.5", "18.7", "18.8", "18.9", "19.2", "21.1", "22.1"] },
    { "id": 19, "tasks": ["18.6", "19.3", "19.4", "21.2", "22.2", "22.3", "23.1"] },
    { "id": 20, "tasks": ["21.3", "21.4", "21.5", "23.2", "25.1"] },
    { "id": 21, "tasks": ["23.3", "23.4", "24.1", "25.2", "26.1"] },
    { "id": 22, "tasks": ["24.2", "25.3", "25.4", "26.2", "27.1"] },
    { "id": 23, "tasks": ["26.3", "27.2", "28.1"] },
    { "id": 24, "tasks": ["26.4", "28.2", "29.1"] },
    { "id": 25, "tasks": ["26.5", "29.2"] },
    { "id": 26, "tasks": ["26.6", "26.9", "29.3", "31.1"] },
    { "id": 27, "tasks": ["26.7", "26.8", "27.3", "28.3", "29.4", "29.5", "31.2"] },
    { "id": 28, "tasks": ["32.1", "32.2", "32.3", "33.1", "33.2", "33.3", "33.4", "33.5", "33.6", "33.7", "33.8", "33.9"] },
    { "id": 29, "tasks": ["33.10"] }
  ]
}
```
