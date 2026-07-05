# Requirements Document

## Introduction

Molt is durable, distributed, governable memory for AI coding agents. A managed CockroachDB cluster is the system of record for every agent session, derived artifact, and provenance edge. AWS supplies compute, key material, tamper-evident object storage, configuration storage, telemetry, and the default model provider. Model inference reaches the chosen provider through an abstraction, so provider availability is a configuration concern rather than an architectural one.

The headline capability is **provable forgetting**. A dev consultancy runs AI coding agents across many client codebases, and every session becomes shared memory readable by agents on any machine. That memory is a shadow copy of each client's source, secrets, and internal architecture, and it blends clients: behavioral baselines and learned procedures are distilled from work across several clients at once. When one client's engagement ends under a contractual purge obligation, Molt locates every artifact derived from that client — including derived artifacts that no longer name the client and pasted code fragments that carry no repository or path identifier — removes or surgically redacts each artifact, and emits a cryptographically signed erasure certificate that an independent third party can verify against the live cluster.

Two problems define the difficulty. **Semantic residue**: proprietary code pasted from one client's codebase into a session on another client's repository carries no identifier, so exact-match search cannot find the fragment and only vector similarity over embeddings can. **Blended derived artifacts**: a behavioral baseline distilled from three clients must have one client's contribution removed while remaining valid for the other two, so deletion is inadequate and surgical rewriting is required.

Memory is on the critical path of agent decisions, not a passive log. Before acting, an agent queries memory for prior attempts at a similar action across the whole fleet and receives the recorded outcomes, which change what the agent does next.

This document also records the delivery obligations of the CockroachDB × AWS "Build with Agentic Memory" hackathon and the clean-room originality constraints of the build, because both are testable acceptance conditions for the submission.

### Scope Boundary

The delivered vertical slice is: **capture → ledger → semantic recall → policy watcher → erasure certificate**. The Out of Scope section lists exclusions explicitly. Anything not stated as an acceptance criterion in this document is out of scope for the submission.

## Glossary

### Domain terms

- **Agent_CLI**: A third-party AI coding agent command-line tool whose public hook specification Molt integrates with. The supported set is Claude Code, Cursor, Codex, Gemini CLI, and Copilot.
- **Session**: One bounded run of an Agent_CLI, identified by a UUID, owning an ordered stream of Events.
- **Event**: One immutable observation within a Session: session start, session end, user prompt, assistant response, tool call, tool result, model request, model response, file read, file write, shell command, decision, error, or cost record.
- **Ledger**: The append-only CockroachDB table of Events. The Ledger is the durable system of record for provenance.
- **Artifact**: Any row of stored memory content that can be erased: an Event, a Session record, a Derived_Artifact, or an Embedding.
- **Derived_Artifact**: Content produced by summarising, distilling, or generalising other Artifacts. The three delivered kinds are Summary, Behavioral_Baseline, and Learned_Procedure.
- **Behavioral_Baseline**: A Derived_Artifact describing statistically normal agent behavior, distilled from Events belonging to one or more Clients.
- **Learned_Procedure**: A Derived_Artifact describing a reusable sequence of agent actions that previously succeeded, distilled from Events belonging to one or more Clients.
- **Blended_Artifact**: A Derived_Artifact whose Client_Bindings name two or more distinct Clients.
- **Client**: A tenant of the consultancy and the data subject of an Erasure_Request. Identified by a UUID and a stable slug.
- **Client_Binding**: A stored edge asserting that a named Artifact contains or is derived from data belonging to a named Client, carrying a detection method and a confidence value.
- **Lineage_Edge**: A stored edge from a Derived_Artifact to one parent Artifact from which the Derived_Artifact was produced.
- **Lineage_Graph**: The directed acyclic graph formed by all Lineage_Edges.
- **Hash_Chain**: A per-Session sequence in which each Ledger row stores the digest of the preceding row in the same Session, making retrospective edits detectable.
- **Embedding**: A fixed-dimension float vector representation of an Artifact's text, stored in a CockroachDB `VECTOR` column.
- **Semantic_Residue**: Content belonging to one Client that is stored under a different Client's scope and carries no identifier of the owning Client, and is therefore discoverable only by vector similarity.
- **Erasure_Request**: An operator-submitted instruction to erase one named Client from memory, identified by a UUID and carrying requester identity and justification.
- **Erasure_Run**: One execution of the Erasure_Engine against one Erasure_Request, recording `t_before`, `t_after`, and per-Artifact Dispositions.
- **Disposition**: The decision and outcome recorded for one Artifact within an Erasure_Run: `hard_delete`, `surgical_redaction`, or `retained`.
- **Surgical_Redaction**: Rewriting a Blended_Artifact so that the erased Client's contribution is removed and the contributions of all other Clients are preserved.
- **Erasure_Certificate**: The signed JSON document attesting to one completed Erasure_Run.
- **Verification_Query**: A SQL statement embedded in an Erasure_Certificate that a third party can execute against the cluster to re-confirm the certificate's central claim.
- **Policy_Rule**: A declarative condition over the memory write stream whose violation triggers a Policy_Action.
- **Policy_Action**: One of `allow`, `warn`, `require_approval`, or `halt_agent`.
- **Kill_Switch**: The mechanism by which the Policy_Watcher marks a Session as halted so that the capture layer on any machine stops that Session's agent.
- **Approval_Queue**: The stored set of pending human decisions raised by Policy_Rules whose Policy_Action is `require_approval`.
- **Auditor**: An untrusted third-party security reviewer acting for a departing Client, granted read-only interrogation access.
- **Jurisdiction**: A named retention regime that determines the retention interval applied to an Artifact.
- **Self_Managed_Backup**: A cluster backup created by issuing a `BACKUP INTO` statement that writes to an operator-owned storage location.
- **Managed_Backup**: A backup created automatically by CockroachDB Cloud on the cluster's own fixed schedule, referenced by identifier and timestamp rather than triggered by Molt.
- **Agent_Skill**: An executable skill definition expressed in the open Agent Skills format, shipped in the Repository and loadable by any MCP-compatible client.
- **CI_Workflow**: The Repository's continuous integration workflow definition, which runs checks and tests on the Repository's own contents.
- **Embedding_Provider**: The interface that accepts a text input and returns a fixed-dimension float vector, implemented once per model provider.
- **Text_Provider**: The interface that accepts a prompt and returns generated text, implemented once per model provider.
- **Stable_Prefix**: The leading portion of a Text_Provider prompt that is identical across every call within one adjudication batch, comprising the task instructions and the query Artifact excerpt.
- **Cache_Boundary**: The position in a Text_Provider prompt immediately following the Stable_Prefix, marked so that a provider supporting prompt caching can reuse the Stable_Prefix across calls.
- **Minimum_Cacheable_Prefix_Length**: The configured least Stable_Prefix length, expressed in bytes, at which the Adjudicator marks a Cache_Boundary, set from the measured shortest prefix that the configured Text_Provider caches.
- **Memory_Tier**: One named class of stored memory, distinguished from the other tiers by what that tier holds, whether that tier is mutable, and which CockroachDB capability that tier relies on. The six tiers are `episodic`, `attribution`, `procedural_semantic`, `provenance`, `action`, and `working`.
- **Working_Memory**: Short-lived agent scratch state held in the `working` Memory_Tier, keyed by Session identifier and scratch key, disposable, and physically deleted by Row-Level TTL.
- **Attribution_Version**: One immutable version of a Client_Binding, carrying a validity interval and an optional reference to the Attribution_Version that supersedes it.
- **Erasure_Lease**: The stored record of exclusive ownership of erasure for one Client, carrying an owner identifier, a Fencing_Generation, an expiry timestamp, and an idempotency key.
- **Fencing_Generation**: A monotonically increasing integer held on an Erasure_Lease and incremented on every takeover, carried on every erasure write so that a write from a superseded owner is refused.
- **Ledger_Checkpoint**: A signed record covering a bounded window of the Ledger, holding a root digest computed over the terminal Hash_Chain digest of every Session in that window.
- **Ingress_Signature**: A keyed digest over a request timestamp and a request body, presented as a request header so that a replayed ingest request is bounded by a maximum request age.
- **Threshold_Grid**: A set of pairs of an auto-inclusion threshold and a review threshold over which the Sensitivity_Analyzer evaluates the residue candidate set.
- **Procedure_Confidence**: A value in the closed interval from 0.0 to 1.0 held on a Derived_Artifact of kind Learned_Procedure, raised by a succeeded outcome and lowered by a failed outcome.
- **Interface_Specification**: The machine-readable document describing every Collector route, every Web_Console route, and every Molt_MCP_Server tool with request shapes, response shapes, authentication requirements, and error responses.
- **Threat_Model**: The document recording the trust boundaries of the delivered configuration, the threats considered, the mitigation applied to each threat, and each threat the design accepts without mitigation.

### System component names

- **Molt**: The complete system comprising all components named below.
- **Capture_Hook**: The component invoked by an Agent_CLI hook that converts a hook payload into Events.
- **MCP_Proxy**: The component that sits between an Agent_CLI and an MCP server and records JSON-RPC traffic in both directions.
- **Decorator_API**: The Python decorator interface for instrumenting application code directly.
- **Redactor**: The component that removes secret material from Event payloads before any write.
- **Collector**: The HTTP service that receives Events and Session metadata from remote machines.
- **Memory_Store**: The CockroachDB data-access layer that owns all schema, transactions, and queries.
- **Embedder**: The component that produces Embeddings through the configured Embedding_Provider.
- **Provider_Selector**: The component that reads provider configuration and constructs the configured Embedding_Provider implementation and the configured Text_Provider implementation.
- **Binding_Detector**: The component that creates Client_Bindings at ingest.
- **Recall_Engine**: The component that answers agent pre-action memory queries.
- **Erasure_Engine**: The component that executes the three erasure phases.
- **Lease_Manager**: The component that grants, renews, and transfers Erasure_Leases and that owns the Fencing_Generation for each Client.
- **Checkpoint_Signer**: The component that computes, signs, stores, and verifies Ledger_Checkpoints.
- **Sensitivity_Analyzer**: The component that evaluates the residue candidate set across a Threshold_Grid and reports the consequence of each threshold pair.
- **Confidence_Tracker**: The component that records Learned_Procedure retrievals and outcomes and that adjusts Procedure_Confidence.
- **Residue_Detector**: The Erasure_Engine sub-component that finds Semantic_Residue by vector similarity.
- **Adjudicator**: The Erasure_Engine sub-component that asks the configured Text_Provider to decide borderline residue candidates.
- **Redaction_Rewriter**: The Erasure_Engine sub-component that asks the configured Text_Provider to perform Surgical_Redaction.
- **Certificate_Builder**: The component that assembles, signs, and stores Erasure_Certificates.
- **Certificate_Verifier**: The component that independently verifies an Erasure_Certificate.
- **Policy_Watcher**: The component that consumes the memory write stream and applies Policy_Rules.
- **Retention_Manager**: The component that configures database-enforced retention.
- **Backup_Manager**: The component that secures pre-erasure backup evidence, either by issuing a Self_Managed_Backup or by recording a reference to the most recent Managed_Backup.
- **Auditor_Gateway**: The read-only access path exposing the cluster to an Auditor through the CockroachDB Managed MCP Server.
- **Molt_MCP_Server**: The MCP server that exposes Molt memory to any MCP-compatible client agent as read-only tools.
- **Web_Console**: The publicly reachable demo web application.
- **CLI**: The `molt` command-line interface.
- **Provisioner**: The ccloud CLI scripts that create the cluster, roles, and service accounts.
- **Seed_Generator**: The component that produces realistic multi-client seed data including deliberate cross-client contamination.
- **Telemetry**: The component that emits metrics and structured logs to Amazon CloudWatch.
- **Repository**: The public source repository constituting the submission.

### External services

- **CockroachDB_Cluster**: The managed CockroachDB Cloud cluster holding all Molt data.
- **Managed_MCP_Server**: The CockroachDB Cloud MCP endpoint at `https://cockroachlabs.cloud/mcp`.
- **ccloud_CLI**: The CockroachDB Cloud command-line tool.
- **Agent_Skills_Repo**: The CockroachDB Agent Skills repository used for schema and query review during development.
- **Bedrock**: Amazon Bedrock, the documented default Embedding_Provider implementation and the documented default Text_Provider implementation.
- **External_Embedding_Service**: The Voyage AI embeddings API, supplying the code-specialised retrieval embedding model used by the delivered demonstration configuration.
- **External_Text_Service**: The Anthropic messages API, supplying the adjudication model and the redaction model used by the delivered demonstration configuration, and supporting prompt caching.
- **KMS**: AWS Key Management Service, holding the asymmetric signing key for Erasure_Certificates.
- **S3_Bucket**: The Amazon S3 bucket with versioning and Object Lock enabled that stores Erasure_Certificates.
- **Parameter_Store**: AWS Systems Manager Parameter Store, standard tier, holding the database connection string, the Collector bearer token, and model provider credentials. The standard tier carries no per-parameter monthly charge.
- **Lambda**: AWS Lambda, hosting the Collector ingest function and the Web_Console function, each reached through an HTTPS function endpoint.
- **Fargate**: Amazon ECS Fargate, hosting the Policy_Watcher and the Molt_MCP_Server, both of which hold long-running connections.
- **CDN_Distribution**: The Amazon CloudFront distribution that terminates HTTPS for the Web_Console using CloudFront's own default certificate and generated hostname.
- **CloudWatch**: Amazon CloudWatch, receiving metrics and structured logs.

## Requirements

### Requirement 1: Agent CLI Hook Capture

**User Story:** As a consultancy engineer running an AI coding agent, I want every session recorded automatically, so that the consultancy holds a complete provenance record without changing how the engineer works.

#### Acceptance Criteria

1. THE Capture_Hook SHALL accept hook payloads from Claude Code, Cursor, Codex, Gemini CLI, and Copilot.
2. WHEN the Capture_Hook receives a hook payload, THE Capture_Hook SHALL emit one or more Events whose fields conform to the Event schema defined in Requirement 7.
3. THE Capture_Hook SHALL derive each Event's Agent_CLI identity from the invoking hook format rather than from operator configuration.
4. WHEN a hook payload identifies a spawned subagent, THE Capture_Hook SHALL populate the child Session's parent Session identifier, spawning Event identifier, and nesting depth.
5. WHEN the Capture_Hook produces the first Event of a Session, THE Capture_Hook SHALL resolve the Client identifier from the configured workspace-to-Client mapping.
6. IF the workspace-to-Client mapping contains no entry for the current workspace, THEN THE Capture_Hook SHALL assign the reserved `unassigned` Client identifier and emit a warning to standard error.
7. IF any step of Capture_Hook processing raises an exception, THEN THE Capture_Hook SHALL write a diagnostic line to standard error and exit with status code 0.
8. THE Capture_Hook SHALL complete within 250 milliseconds of wall-clock time per invocation, measured at the 95th percentile across a 1000-invocation benchmark.
9. THE Capture_Hook SHALL implement each Agent_CLI hook format from that tool's own public specification.

### Requirement 2: MCP Traffic Capture

**User Story:** As a governance owner, I want MCP tool traffic recorded transparently, so that memory includes what external tools returned to agents without requiring changes to the agent or the tool server.

#### Acceptance Criteria

1. THE MCP_Proxy SHALL support stdio transport and HTTP transport between an Agent_CLI and an MCP server.
2. WHEN the MCP_Proxy relays a JSON-RPC request from an Agent_CLI to an MCP server, THE MCP_Proxy SHALL emit a tool call Event carrying the method name, the request identifier, and the redacted parameters.
3. WHEN the MCP_Proxy relays a JSON-RPC response from an MCP server to an Agent_CLI, THE MCP_Proxy SHALL emit a tool result Event linked to the tool call Event by request identifier.
4. THE MCP_Proxy SHALL forward every relayed message byte-for-byte identically to the message received, excluding transport framing.
5. IF Event emission fails inside the MCP_Proxy, THEN THE MCP_Proxy SHALL continue relaying traffic and increment a dropped-Event counter.
6. WHEN an MCP server closes the connection, THE MCP_Proxy SHALL close the corresponding Agent_CLI connection and emit a session end Event.

### Requirement 3: Direct Instrumentation API

**User Story:** As a developer building an agentic Python service, I want to record my own agent's activity into Molt, so that in-house agents share the same memory as the vendor CLIs.

#### Acceptance Criteria

1. THE Decorator_API SHALL expose a decorator that records one tool call Event before the wrapped callable executes and one tool result Event after the wrapped callable returns.
2. WHEN a decorated callable raises an exception, THE Decorator_API SHALL emit an error Event carrying the exception type and the redacted exception message, and SHALL re-raise the original exception.
3. THE Decorator_API SHALL expose a context manager that opens a Session on entry and closes that Session on exit.
4. THE Decorator_API SHALL record the wall-clock duration of each decorated call in milliseconds.
5. IF Molt is unconfigured, THEN THE Decorator_API SHALL execute the wrapped callable and return the wrapped callable's result without emitting Events.

### Requirement 4: Secret Redaction Before Write

**User Story:** As a security owner, I want secrets removed before memory is written, so that the memory layer never becomes a credential store.

#### Acceptance Criteria

1. THE Redactor SHALL replace detected secret material in an Event payload with a fixed placeholder token before the Event leaves the machine that produced the Event.
2. THE Redactor SHALL detect at minimum: AWS access key identifiers, AWS secret access keys, private key blocks, bearer tokens, database connection strings containing credentials, and values of environment variables whose names match a configured sensitive-name pattern set.
3. WHEN the Redactor modifies an Event payload, THE Redactor SHALL set that Event's `redacted` flag to true.
4. THE Redactor SHALL preserve the structure, key names, and value types of the payload that the Redactor modifies.
5. THE Redactor SHALL apply redaction recursively to nested mappings and sequences to a configured maximum depth of 32 levels.
6. WHERE the operator sets the redaction-disabled configuration flag, THE Redactor SHALL pass payloads through unmodified and THE Telemetry SHALL emit a warning log record naming the affected Session.
7. THE Redactor SHALL complete redaction of a 256 KiB payload within 50 milliseconds.

### Requirement 5: Remote Event Collection

**User Story:** As a consultancy operating agents on many machines, I want events streamed to a central collector, so that memory is shared fleet-wide rather than trapped on laptops.

#### Acceptance Criteria

1. THE Collector SHALL expose an HTTP endpoint that accepts a batch of newline-delimited Event records.
2. THE Collector SHALL expose an HTTP endpoint that creates or updates Session metadata.
3. THE Collector SHALL expose an unauthenticated HTTP health endpoint that reports liveness and reports no memory content.
4. THE Collector SHALL require a bearer token on every endpoint other than the health endpoint, and SHALL respond with status code 401 to a request whose bearer token is absent or does not match.
5. THE Collector SHALL compare the presented bearer token against the expected token using a constant-time comparison.
6. WHEN the Collector accepts a batch containing at least one malformed record, THE Collector SHALL persist every well-formed record in that batch and SHALL return the accepted count and the rejected count.
7. WHEN the Collector persists an Event whose Session does not yet exist, THE Collector SHALL create the Session record within the same database transaction as the Event.
8. THE Collector SHALL retrieve the database connection string and the expected bearer token from Parameter_Store at cold start.
9. IF the Collector cannot reach the CockroachDB_Cluster, THEN THE Collector SHALL respond with status code 503 and SHALL emit a CloudWatch metric named `collector.write_failure`.
10. THE Collector SHALL enforce a configured maximum request body size whose default value is 5 MiB.
11. IF a request body exceeds the configured maximum request body size, THEN THE Collector SHALL respond with status code 413 and SHALL persist no record from that request.
12. THE Collector SHALL run under a reserved concurrency ceiling whose configured default value is 10 concurrent executions, so that the cost of requests presenting a leaked bearer token is bounded.
13. THE Collector SHALL require an Ingress_Signature in addition to the bearer token on the Event batch endpoint of criterion 1 and on the Session metadata endpoint of criterion 2, as specified in Requirement 47.

### Requirement 6: Capture Resilience

**User Story:** As an engineer whose deadline depends on the agent, I want capture failures to be invisible, so that memory instrumentation never costs me a session.

#### Acceptance Criteria

1. IF the Collector is unreachable, THEN THE Capture_Hook SHALL buffer the affected Events to a local spool file and exit with status code 0.
2. WHEN the Capture_Hook starts and the local spool file is non-empty, THE Capture_Hook SHALL attempt to transmit spooled Events before transmitting new Events.
3. THE Capture_Hook SHALL retry a failed transmission at most 3 times with exponential backoff starting at 200 milliseconds.
4. THE Capture_Hook SHALL bound every network operation with a timeout of 5 seconds.
5. WHEN the local spool file reaches 64 MiB, THE Capture_Hook SHALL discard the oldest spooled Events to remain within that bound and SHALL record the discarded count.
6. THE Capture_Hook SHALL exit with status code 0 for every input, including malformed hook payloads and absent configuration.

### Requirement 7: CockroachDB-First Event Ledger

**User Story:** As a governance owner, I want an append-only ledger in a distributed database, so that provenance survives machine loss and is queryable from anywhere.

#### Acceptance Criteria

1. THE Memory_Store SHALL store every Event as one row of an append-only Ledger table in the CockroachDB_Cluster.
2. THE Memory_Store SHALL define the Ledger primary key as a `UUID` column populated with a version-4 UUID.
3. THE Memory_Store SHALL define a non-null Client identifier column on the Ledger table in the first schema migration.
4. THE Memory_Store SHALL store every Event timestamp in a `TIMESTAMPTZ` column.
5. THE Memory_Store SHALL store Event payloads in a `JSONB` column.
6. THE Memory_Store SHALL grant no role the privilege to execute `UPDATE` against the Ledger table.
7. THE Memory_Store SHALL support the Event categories: session start, session end, user prompt, assistant response, tool call, tool result, model request, model response, file read, file write, shell command, decision, error, and cost record.
8. THE Memory_Store SHALL express the relationship between a result Event and the calling Event through a parent Event identifier column rather than through payload nesting.
9. THE Memory_Store SHALL define an index that serves retrieval of all Events for one Session ordered by sequence number.
10. THE Memory_Store SHALL define an index that serves retrieval of all Events for one Client ordered by timestamp.

### Requirement 8: Transactional Hash Chain

**User Story:** As an auditor, I want tamper evidence on the ledger, so that a retrospective edit to memory is detectable rather than silent.

#### Acceptance Criteria

1. THE Memory_Store SHALL store on each Ledger row a sequence number that is unique within that row's Session.
2. THE Memory_Store SHALL store on each Ledger row a digest of that row's canonical content and the digest of the preceding row in the same Session.
3. THE Memory_Store SHALL compute each row digest with SHA-256.
4. THE Memory_Store SHALL compute the row digest inside the same database transaction that inserts the row, using a value returned by that transaction's own statements.
5. WHEN two concurrent transactions insert Events into the same Session, THE Memory_Store SHALL assign distinct sequence numbers and SHALL produce a Hash_Chain in which each row references exactly one predecessor.
6. THE Memory_Store SHALL expose a chain verification operation that recomputes every digest for a Session and reports the sequence number of the first mismatch.
7. WHEN the chain verification operation finds no mismatch, THE Memory_Store SHALL report the verified row count and the terminal digest.

### Requirement 9: Session Metadata, Tenancy, and Subagent Lineage

**User Story:** As a team lead, I want sessions attributed to a client, a team, and a parent agent, so that cost, behavior, and erasure scope can be reasoned about per client.

#### Acceptance Criteria

1. THE Memory_Store SHALL store for each Session a `UUID` identifier, a non-null Client identifier, a start timestamp, an Agent_CLI name, and a machine identifier.
2. THE Memory_Store SHALL store for each Session an optional parent Session identifier, an optional spawning Event identifier, and a non-negative nesting depth.
3. THE Memory_Store SHALL set the nesting depth of a Session with no parent Session to 0.
4. WHEN a Session records a parent Session, THE Memory_Store SHALL set that Session's nesting depth to the parent Session's nesting depth plus 1.
5. THE Memory_Store SHALL store for each Session a team identifier and an attribution mapping naming the initiating principal.
6. THE Memory_Store SHALL store for each Session the running counts of tool call Events, model request Events, error Events, consumed tokens, and accrued cost.
7. THE Memory_Store SHALL reject an insert of a Session whose parent Session identifier does not reference an existing Session.
8. THE Memory_Store SHALL scope every read query for Sessions, Events, and Derived_Artifacts by Client identifier.
9. THE Memory_Store SHALL define the foreign key from a Session's spawning Event identifier to the Ledger table as a constraint added after both tables exist and validated after that addition, because the CockroachDB_Cluster implements no deferred constraint checking.
10. WHEN one transaction writes a spawning Event together with the child Session that Event spawns, THE Memory_Store SHALL insert the spawning Event before the child Session, because the CockroachDB_Cluster checks each foreign key per statement rather than at commit.

### Requirement 10: Embeddings and Distributed Vector Index

**User Story:** As an agent, I want memory searchable by meaning, so that similar prior work is findable when no keyword matches.

#### Acceptance Criteria

1. THE Embedder SHALL produce Embeddings through the configured Embedding_Provider of Requirement 37.
2. THE Memory_Store SHALL store each Embedding in a CockroachDB `VECTOR` column of fixed dimension 1024.
3. THE Memory_Store SHALL create a CockroachDB distributed vector index on the Embedding column using the `vector_l2_ops` operator class.
4. THE Memory_Store SHALL store each Embedding row in the same CockroachDB_Cluster as the Ledger and the Derived_Artifact tables.
5. WHEN the Memory_Store writes an Artifact that carries embeddable text, THE Memory_Store SHALL write the Artifact row and the corresponding Embedding row in one transaction.
6. THE Memory_Store SHALL store on each Embedding row the identifier of the source Artifact, the source Artifact kind, the Client identifier, and the embedding model identifier.
7. THE Memory_Store SHALL answer a nearest-neighbour query returning the identifier, the Client identifier, and the cosine distance of each of the k closest Embeddings.
8. IF the configured Embedding_Provider returns an error for an embedding call, THEN THE Embedder SHALL retry at most 3 times with exponential backoff and SHALL then record the Artifact in a pending-embedding state.
9. WHILE an Artifact is in the pending-embedding state, THE Memory_Store SHALL include that Artifact in explicit sweep results and SHALL report that Artifact in the unembedded-coverage count of any Erasure_Certificate.
10. THE Embedder SHALL scale every Embedding to unit L2 norm before the Memory_Store writes that Embedding, so that cosine distance thresholds and the `vector_l2_ops` index ordering coincide.
11. WHERE the CockroachDB_Cluster tier provides no distributed vector index, THE Memory_Store SHALL answer nearest-neighbour queries by exact scan over the Embedding column and THE Telemetry SHALL emit a `store.vector_index_unavailable` metric.
12. THE Repository documentation SHALL record that the distributed vector index is created on the delivered cluster tier and that the exact-scan path exists only as a fallback for tiers on which the index cannot be created.
13. THE Repository documentation SHALL record, per Embedding_Provider implementation, whether that implementation returns unit-normalised vectors, so that the correctness argument of criterion 10 names the implementation it depends on rather than assuming that dependency.
14. THE property-based test set SHALL include an Embedding_Provider stub returning vectors whose L2 norm differs from 1, so that the scaling of criterion 10 is exercised rather than bypassed by an implementation that already returns unit-normalised vectors.
15. THE Memory_Store SHALL store on each Embedding row an assertion that the stored vector is unit-normalised, so that a vector reaching the Embedding table through a path skipping the scaling of criterion 10 is identifiable rather than merely suspected.

### Requirement 11: Lineage Graph for Derived Artifacts

**User Story:** As a governance owner, I want every derived artifact to record its parents, so that erasure can follow derivation chains no matter how deep.

#### Acceptance Criteria

1. THE Memory_Store SHALL store each Derived_Artifact with a `UUID` identifier, a kind, a text body, a content digest, and a creation timestamp.
2. WHEN the Memory_Store writes a Derived_Artifact, THE Memory_Store SHALL write one Lineage_Edge per parent Artifact in the same transaction as the Derived_Artifact.
3. THE Memory_Store SHALL reject a Lineage_Edge whose parent Artifact identifier does not reference an existing Artifact.
4. THE Memory_Store SHALL reject a Lineage_Edge whose insertion would make the Lineage_Graph cyclic.
5. THE Memory_Store SHALL expose a descendant query that returns every Artifact reachable from a given set of Artifacts by following Lineage_Edges in the parent-to-child direction, implemented as a recursive common table expression.
6. THE Memory_Store SHALL expose an ancestor query that returns every Artifact from which a given Artifact was derived.
7. THE descendant query and the ancestor query SHALL each terminate on a Lineage_Graph containing at least 100000 edges within 5 seconds.
8. THE Memory_Store SHALL store on each Lineage_Edge the derivation method name that produced the child Artifact.

### Requirement 12: Client Bindings Detected at Ingest

**User Story:** As a governance owner, I want each artifact bound to the clients whose data it contains at the moment of writing, so that erasure scope is known before erasure is requested.

#### Acceptance Criteria

1. WHEN the Memory_Store writes an Artifact, THE Binding_Detector SHALL create one Client_Binding per Client whose data the Binding_Detector detects in that Artifact.
2. THE Binding_Detector SHALL create a Client_Binding with detection method `scope` for the Client that owns the Session under which the Artifact was produced.
3. THE Binding_Detector SHALL create a Client_Binding with detection method `inherited` for every Client bound to any parent Artifact of a Derived_Artifact.
4. THE Binding_Detector SHALL create a Client_Binding with detection method `marker` for every Client whose configured content markers appear in the Artifact text.
5. THE Binding_Detector SHALL store on each Client_Binding a confidence value in the closed interval from 0.0 to 1.0.
6. THE Memory_Store SHALL write Client_Bindings in the same transaction as the Artifact those Client_Bindings describe.
7. THE Memory_Store SHALL enforce uniqueness of the pair of Artifact identifier and Client identifier across Attribution_Versions carrying no superseding reference, and SHALL retain the highest confidence value submitted for a repeated pair as the confidence of the current Attribution_Version, as specified in Requirement 43.
8. THE Memory_Store SHALL expose a query returning every Artifact carrying a Client_Binding for a named Client.

### Requirement 13: Semantic Recall on the Agent Critical Path

**User Story:** As an agent about to take an action, I want to know whether any agent anywhere has attempted something similar and how that attempt ended, so that my next action is informed by fleet-wide experience.

#### Acceptance Criteria

1. THE Recall_Engine SHALL accept a natural-language description of an intended action and return the k most similar prior Artifacts ordered by ascending cosine distance.
2. THE Recall_Engine SHALL return with each result the outcome classification of the originating Session, drawn from `succeeded`, `failed`, and `abandoned`.
3. THE Recall_Engine SHALL return with each result the originating Session identifier, the originating machine identifier, and the originating timestamp.
4. THE Recall_Engine SHALL exclude from results every Artifact whose Client_Bindings name only Clients absent from the caller's permitted Client set.
5. THE Recall_Engine SHALL return results within 2 seconds at the 95th percentile for a corpus of at least 100000 Embeddings.
6. WHEN an Agent_CLI pre-action hook invokes the Recall_Engine, THE Capture_Hook SHALL return the recall results to the Agent_CLI in that hook's documented context-injection format.
7. THE Recall_Engine SHALL emit one recall Event recording the query text, the returned Artifact identifiers, and the returned distances.
8. IF the Recall_Engine cannot reach the CockroachDB_Cluster, THEN THE Capture_Hook SHALL return an empty result set to the Agent_CLI and exit with status code 0.
9. THE CLI SHALL expose a `recall` verb that performs the same query and prints the results with distances and outcomes.

### Requirement 14: Database-Enforced Retention

**User Story:** As a compliance owner, I want retention enforced by the database, so that expiry does not depend on an operator remembering to run a job.

#### Acceptance Criteria

1. THE Retention_Manager SHALL configure CockroachDB Row-Level TTL on the Ledger table, the Derived_Artifact table, the Embedding table, and the Working_Memory table of Requirement 42.
2. THE Memory_Store SHALL store on each Artifact an expiry timestamp column consulted by Row-Level TTL.
3. WHEN the Memory_Store writes an Artifact, THE Memory_Store SHALL set that Artifact's expiry timestamp to the write timestamp plus the retention interval of the Artifact's Jurisdiction.
4. THE Retention_Manager SHALL support a distinct retention interval per Jurisdiction, configured per Client.
5. THE Retention_Manager SHALL report for each Client the configured Jurisdiction, the retention interval, the count of Artifacts expiring within 7 days, and the count of already-expired Artifacts.
6. THE Retention_Manager SHALL depend on no scheduled process outside the CockroachDB_Cluster to delete expired rows.
7. WHEN the Retention_Manager configures Row-Level TTL on a table, THE Retention_Manager SHALL read that table's stored configuration back and SHALL confirm that the configured Row-Level TTL parameters are present, because the CockroachDB_Cluster reports success for a Row-Level TTL configuration applied to a table created earlier in the same transaction while storing no such parameter.
8. IF the read-back of criterion 7 reports the Row-Level TTL parameters absent, THEN THE Retention_Manager SHALL raise an error naming the affected table, because the underlying failure is silent and would otherwise leave a Memory_Tier that expires no row.

### Requirement 15: Serializable Isolation Against In-Flight Erasure

**User Story:** As a governance owner, I want concurrent agent writes unable to slip new artifacts past an erasure that is already running, so that an erasure certificate cannot be defeated by a race.

#### Acceptance Criteria

1. THE Memory_Store SHALL execute every write transaction under the SERIALIZABLE isolation level.
2. THE Erasure_Engine SHALL execute each Erasure_Run's sweep, disposition, and completion recording under the SERIALIZABLE isolation level.
3. WHILE an Erasure_Run for a Client is in progress, IF a concurrent transaction writes an Artifact carrying a Client_Binding for that Client, THEN THE Memory_Store SHALL abort exactly one of the two transactions with a serialization error.
4. WHEN the Memory_Store receives a serialization error, THE Memory_Store SHALL retry the aborted transaction at most 5 times with exponential jittered backoff.
5. IF a write transaction still fails after the configured retry limit, THEN THE Memory_Store SHALL raise an error naming the transaction and THE Telemetry SHALL emit a `store.serialization_exhausted` metric.
6. THE Memory_Store SHALL update Session counters with a single read-modify-write-free statement so that concurrent counter updates lose no increments.
7. THE Memory_Store SHALL serialise writes without acquiring any process-level or cluster-global lock shared across unrelated Sessions.

### Requirement 16: Erasure Phase One — Explicit Sweep

**User Story:** As a governance owner, I want everything explicitly linked to a departing client found first, so that the cheap and certain part of erasure is complete before inference begins.

#### Acceptance Criteria

1. WHEN the Erasure_Engine begins an Erasure_Run, THE Erasure_Engine SHALL record the run identifier, the Erasure_Request identifier, the Client identifier, the requester identity, and `t_before` as the cluster's current timestamp.
2. THE Erasure_Engine SHALL select every Session whose Client identifier equals the named Client.
3. THE Erasure_Engine SHALL select every Event belonging to a selected Session.
4. THE Erasure_Engine SHALL select every Artifact carrying a Client_Binding for the named Client.
5. THE Erasure_Engine SHALL select every Artifact reachable from the previously selected Artifacts by the descendant query of Requirement 11.
6. THE Erasure_Engine SHALL select every Embedding whose source Artifact appears in the selected set.
7. THE Erasure_Engine SHALL record the explicit sweep result as a candidate set in which each entry names the Artifact identifier, the Artifact kind, the content digest, and the selection reason.
8. THE Erasure_Engine SHALL complete the explicit sweep of a corpus of 100000 Artifacts within 60 seconds.

### Requirement 17: Erasure Phase Two — Semantic Residue Detection

**User Story:** As a governance owner, I want pasted client code found even when nothing names the client, so that the purge covers material that grep cannot reach.

#### Acceptance Criteria

1. THE Residue_Detector SHALL build one or more query Embeddings from the text of Artifacts explicitly bound to the named Client.
2. THE Residue_Detector SHALL search the vector index for Embeddings whose cosine distance to a query Embedding is at most the configured review threshold.
3. THE Residue_Detector SHALL exclude from residue candidates every Artifact already present in the explicit sweep candidate set.
4. WHERE a residue candidate's cosine distance is at most the configured auto-inclusion threshold, THE Residue_Detector SHALL mark that candidate as included without invoking the Adjudicator.
5. WHERE a residue candidate's cosine distance is greater than the auto-inclusion threshold and at most the review threshold, THE Adjudicator SHALL classify that candidate as `include` or `exclude`.
6. THE Adjudicator SHALL invoke the configured Text_Provider and SHALL record the provider name, the model identifier, the prompt digest, the returned classification, and the returned reasoning text for each adjudicated candidate.
7. THE Residue_Detector SHALL record for every candidate the cosine distance, the threshold comparison outcome, and the final inclusion decision.
8. IF the configured Text_Provider is unavailable during adjudication, THEN THE Adjudicator SHALL classify the affected candidates as `include` and SHALL record the reason `adjudication_unavailable_fail_closed`.
9. THE Residue_Detector SHALL expose the same search through a CLI `residue` verb that prints candidates with distances and decisions and performs no mutation.
10. THE default auto-inclusion threshold SHALL be a cosine distance of 0.20 and the default review threshold SHALL be a cosine distance of 0.45, and THE Erasure_Engine SHALL accept an operator override for each threshold.

### Requirement 18: Erasure Phase Three — Per-Artifact Disposition

**User Story:** As a governance owner, I want blended artifacts surgically rewritten rather than destroyed, so that removing one client does not degrade memory for the clients that remain.

#### Acceptance Criteria

1. WHERE an included Artifact carries Client_Bindings naming only the erased Client, THE Erasure_Engine SHALL record the Disposition `hard_delete` and SHALL delete that Artifact row, that Artifact's Embedding rows, and that Artifact's Lineage_Edges.
2. WHERE an included Artifact carries Client_Bindings naming the erased Client and at least one other Client, THE Erasure_Engine SHALL record the Disposition `surgical_redaction`.
3. WHEN the Erasure_Engine records the Disposition `surgical_redaction` for an Artifact, THE Redaction_Rewriter SHALL invoke the configured Text_Provider to produce a replacement body with the erased Client's contribution removed and the remaining Clients' contributions preserved.
4. WHEN the Redaction_Rewriter returns a replacement body, THE Erasure_Engine SHALL write the replacement body, recompute the content digest, produce a replacement Embedding, delete the Lineage_Edges to parents bound solely to the erased Client, and remove the erased Client's Client_Binding, in one transaction.
5. THE Erasure_Engine SHALL retain the pre-redaction content digest and the post-redaction content digest for each surgically redacted Artifact.
6. THE Erasure_Engine SHALL store no pre-redaction content body after the Erasure_Run completes.
7. IF the Redaction_Rewriter cannot produce a replacement body, THEN THE Erasure_Engine SHALL record the Disposition `hard_delete` for that Artifact and SHALL record the reason `redaction_unavailable_fail_closed`.
8. THE Erasure_Engine SHALL record the Disposition `retained` with a reason for every candidate the Erasure_Engine leaves unchanged.
9. WHEN every Disposition is recorded, THE Erasure_Engine SHALL record `t_after` as the cluster's current timestamp.
10. THE Erasure_Engine SHALL expose the run through a CLI `erase` verb that accepts a Client identifier, a requester identity, and a justification.
11. WHERE the operator passes a dry-run flag to the `erase` verb, THE Erasure_Engine SHALL compute and print all Dispositions and SHALL perform no mutation.

### Requirement 19: Pre-Erasure Backup

**User Story:** As a governance owner, I want a cluster backup captured immediately before each erasure, so that survival of unrelated data can be demonstrated rather than asserted.

#### Acceptance Criteria

1. WHEN the Erasure_Engine begins an Erasure_Run, THE Backup_Manager SHALL issue a Self_Managed_Backup as a `BACKUP INTO` statement targeting the operator-owned S3_Bucket before the first mutation of that run.
2. WHEN the Backup_Manager issues a Self_Managed_Backup, THE Backup_Manager SHALL record the backup target URI, the `BACKUP INTO` statement issued, the backup timestamp, and the backup path value `self_managed`.
3. IF no backup path succeeds and the operator has passed no skip-backup flag, THEN THE Erasure_Engine SHALL abort the Erasure_Run before performing any mutation and SHALL report the backup failure.
4. WHERE the operator passes an explicit skip-backup flag, THE Erasure_Engine SHALL proceed and THE Certificate_Builder SHALL record the absent backup in the Erasure_Certificate.
5. THE Memory_Store SHALL probe the availability of the Self_Managed_Backup path once at process start and SHALL record the probe result in the same capability record that holds the results of the other capability probes.
6. IF the capability record reports the Self_Managed_Backup path as unavailable, THEN THE Backup_Manager SHALL retrieve the identifier and the timestamp of the most recent Managed_Backup through the ccloud_CLI, SHALL record the ccloud_CLI command invoked, and SHALL record the backup path value `managed_referenced`.
7. WHERE the Backup_Manager records the backup path value `managed_referenced`, THE Backup_Manager SHALL mark the backup record as referenced rather than taken.
8. THE Repository documentation SHALL record that Managed_Backups on the CockroachDB_Cluster's tier run on a fixed 24-hour schedule with a 30-day retention interval that Molt leaves unaltered, and that the Self_Managed_Backup path is therefore the primary path.

### Requirement 20: Point-in-Time Before and After Proof

**User Story:** As an auditor, I want to compare memory immediately before and after an erasure, so that the change set is visible rather than described.

#### Acceptance Criteria

1. THE Memory_Store SHALL expose a historical read that executes a query with `AS OF SYSTEM TIME` at a caller-supplied timestamp.
2. WHEN the Certificate_Builder assembles an Erasure_Certificate, THE Certificate_Builder SHALL derive the count of Artifacts bound to the erased Client as of `t_before` and as of `t_after` from the append-only Ledger and the recorded Dispositions, and SHALL record the derivation method used.
3. THE Certificate_Builder SHALL record in each Erasure_Certificate that historical reads are bounded by the cluster garbage-collection interval.
4. THE Certificate_Builder SHALL record in each Erasure_Certificate that the append-only Ledger and the recorded Dispositions are the durable evidence for long-horizon provenance and that historical reads are a corroborating convenience layer.
5. IF a requested historical timestamp precedes the cluster garbage-collection horizon, THEN THE Memory_Store SHALL return an error naming the horizon and SHALL perform no fallback read at a different timestamp.
6. WHERE `t_before` and `t_after` both fall within the cluster garbage-collection horizon at the time the Erasure_Certificate is assembled, THE Certificate_Builder SHALL additionally execute the historical read and SHALL record agreement or disagreement with the derived counts.
7. THE Repository documentation SHALL record the measured cluster garbage-collection horizon of 4500 seconds and SHALL state that the derived count mechanism of criterion 2 is the primary mechanism because that horizon is shorter than the evidence lifetime of an Erasure_Certificate.
8. WHERE an Erasure_Certificate's `t_before` precedes the cluster garbage-collection horizon at verification time, THE Certificate_Verifier SHALL confirm the before-state and after-state counts through the derived mechanism of criterion 2 and SHALL report the mechanism used.

### Requirement 21: Erasure Certificate Generation

**User Story:** As a departing client, I want a signed certificate of erasure, so that the consultancy's claim is independently checkable evidence rather than a promise.

#### Acceptance Criteria

1. WHEN an Erasure_Run completes, THE Certificate_Builder SHALL assemble one Erasure_Certificate as a JSON document.
2. THE Erasure_Certificate SHALL contain the Erasure_Request identifier, the requester identity, the justification text, and the erased Client identifier and slug.
3. THE Erasure_Certificate SHALL contain `t_before` and `t_after` as RFC 3339 timestamps with timezone offsets.
4. THE Erasure_Certificate SHALL contain one entry per touched Artifact naming the Artifact identifier, the Artifact kind, the Disposition, the pre-Disposition content digest, and the post-Disposition content digest where a post-Disposition body exists.
5. THE Erasure_Certificate SHALL contain the Lineage_Graph subgraph covering every touched Artifact as an explicit edge list.
6. THE Erasure_Certificate SHALL contain every residue candidate with the cosine distance, the inclusion decision, and, where the Adjudicator ran, the adjudication reasoning text and the model identifier.
7. THE Erasure_Certificate SHALL contain the pre-erasure backup identifier where a backup exists.
8. THE Erasure_Certificate SHALL contain at least one Verification_Query whose result set is empty when erasure is complete.
9. THE Erasure_Certificate SHALL contain the terminal Hash_Chain digest of every Session touched by the Erasure_Run.
10. THE Erasure_Certificate SHALL contain the cluster audit log records covering the Erasure_Run window, retrieved through the ccloud_CLI.
11. THE Certificate_Builder SHALL serialise the certificate payload with a canonical form in which object keys are sorted and no insignificant whitespace appears.
12. THE Certificate_Builder SHALL sign the digest of the canonical payload with an asymmetric KMS key and SHALL attach the signature, the KMS key identifier, and the signing algorithm name.
13. THE Certificate_Builder SHALL store the signed certificate in the S3_Bucket with versioning enabled and Object Lock in `GOVERNANCE` mode with a retention interval whose configured default value for the delivered configuration is 1 day.
14. THE Certificate_Builder SHALL store the certificate object key, the object version identifier, and the payload digest in the CockroachDB_Cluster.
15. IF the S3 write fails, THEN THE Certificate_Builder SHALL retain the signed certificate in the CockroachDB_Cluster and SHALL report the storage failure.
16. THE Repository documentation SHALL record Object Lock `COMPLIANCE` mode as the production posture and SHALL record that the delivered configuration uses `GOVERNANCE` mode with a short retention interval because a `COMPLIANCE` mode retention interval can be overridden by no principal and would leave teardown permanently blocked.

### Requirement 22: Independent Certificate Verification

**User Story:** As a departing client's security reviewer, I want to verify the certificate myself, so that trust rests on cryptography and live queries rather than on the consultancy's word.

#### Acceptance Criteria

1. THE Certificate_Verifier SHALL accept an Erasure_Certificate from a local file path or from an S3 object key.
2. THE Certificate_Verifier SHALL recompute the canonical payload digest and SHALL verify the signature against the public key retrieved from KMS.
3. IF the recomputed digest differs from the signed digest, THEN THE Certificate_Verifier SHALL report `signature_invalid` and SHALL exit with a non-zero status code.
4. THE Certificate_Verifier SHALL execute every Verification_Query in the certificate against a live CockroachDB_Cluster and SHALL report each query's row count.
5. IF a Verification_Query returns at least one row, THEN THE Certificate_Verifier SHALL report `erasure_incomplete` and SHALL list the returned Artifact identifiers.
6. THE Certificate_Verifier SHALL re-execute the before-state and after-state counts and SHALL report agreement or disagreement with the counts recorded in the certificate.
7. THE Certificate_Verifier SHALL verify the Hash_Chain of every Session named in the certificate and SHALL report the first mismatching sequence number where a mismatch exists.
8. THE Certificate_Verifier SHALL operate using a read-only database role.
9. THE Certificate_Verifier SHALL report a machine-readable overall outcome of `verified` or `failed` together with the list of failed checks.
10. THE CLI SHALL expose the verifier as an `attest verify` verb.
11. THE Certificate_Verifier SHALL complete verification of a certificate covering 1000 Artifacts within 30 seconds.

### Requirement 23: Changefeed-Driven Policy Watcher

**User Story:** As a governance owner, I want policy evaluated against the live memory write stream, so that a violating agent on someone else's laptop can be stopped fleet-wide.

#### Acceptance Criteria

1. THE Policy_Watcher SHALL consume memory mutations from a CockroachDB changefeed over the Ledger table and the Derived_Artifact table.
2. THE Policy_Watcher SHALL consume mutations through the sinkless changefeed form `EXPERIMENTAL CHANGEFEED FOR` as the required primary consumption mechanism.
3. IF the CockroachDB_Cluster rejects the changefeed statement, THEN THE Policy_Watcher SHALL fall back to polling the Ledger by ascending timestamp and SHALL emit a `watcher.degraded_to_polling` metric.
4. THE Policy_Watcher SHALL evaluate each consumed mutation against every enabled Policy_Rule.
5. THE Policy_Watcher SHALL support Policy_Rules matching on file path patterns, shell command patterns, Client identifiers, accrued Session cost, and error rate within a Session.
6. WHEN a Policy_Rule with Policy_Action `halt_agent` matches, THE Policy_Watcher SHALL mark the offending Session as halted in the CockroachDB_Cluster within 10 seconds of the mutation timestamp.
7. WHEN the Capture_Hook observes that the current Session is marked halted, THE Capture_Hook SHALL return the Agent_CLI's documented blocking response and SHALL emit a policy halt Event.
8. WHEN a Policy_Rule with Policy_Action `require_approval` matches, THE Policy_Watcher SHALL insert one Approval_Queue entry naming the Session, the matched rule, and the triggering mutation.
9. WHILE an Approval_Queue entry for a Session is pending, THE Capture_Hook SHALL return the Agent_CLI's documented blocking response for actions matching the same rule.
10. WHEN an operator resolves an Approval_Queue entry, THE Policy_Watcher SHALL record the resolving principal, the decision, and the resolution timestamp.
11. THE Policy_Watcher SHALL detect sensitive path access using a configured path pattern set that includes credential files, key material directories, and environment files.
12. THE Policy_Watcher SHALL expose a liveness endpoint reporting the last consumed mutation timestamp.
13. THE CLI SHALL expose the watcher as a `watch` verb.
14. THE Provisioner SHALL confirm that the CockroachDB_Cluster's rangefeed cluster setting is enabled and SHALL record that result in the same capability record that holds the results of the other capability probes.
15. THE Repository documentation SHALL record that sinkless changefeeds are permitted on the delivered cluster with the rangefeed cluster setting enabled, and that the polling path of criterion 3 is retained only as a fallback for clusters on which the changefeed statement is rejected.

### Requirement 24: Read-Only Auditor Access Through the Managed MCP Server

**User Story:** As a departing client's security reviewer, I want to interrogate the cluster in natural language from my own editor, so that I can satisfy myself that nothing still references my organisation.

#### Acceptance Criteria

1. THE Auditor_Gateway SHALL expose the CockroachDB_Cluster to an Auditor through the Managed_MCP_Server at `https://cockroachlabs.cloud/mcp`.
2. THE Auditor_Gateway SHALL use a database role holding `SELECT` privilege only, granted on an explicit list of tables and views.
3. THE Auditor_Gateway SHALL provide connection instructions for Claude Code, Cursor, and Visual Studio Code.
4. THE Provisioner SHALL create one ccloud service account per Auditor with an expiry interval of at most 30 days.
5. THE Auditor_Gateway SHALL restrict an Auditor's visible rows to those bound to that Auditor's own Client through a view that filters by Client identifier.
6. THE Telemetry SHALL record every Auditor query as a structured log record naming the service account, the statement digest, and the row count returned.
7. THE Repository documentation SHALL state that read-only access is the intended posture for an untrusted third party.

### Requirement 25: Public Demo Web Console

**User Story:** As a judge, I want a reachable web application that shows the memory layer working, so that the claims in the submission are observable rather than described.

#### Acceptance Criteria

1. THE Web_Console SHALL be reachable over HTTPS at the CDN_Distribution's provider-supplied default hostname, requiring no custom domain and no ACM certificate.
2. THE Web_Console SHALL display live Sessions across machines with Client, Agent_CLI, machine identifier, nesting depth, Event count, and accrued cost.
3. THE Web_Console SHALL display the Lineage_Graph showing one Client's Sessions feeding shared Derived_Artifacts.
4. THE Web_Console SHALL provide a semantic residue search view that displays candidate Artifacts with cosine distances.
5. THE Web_Console SHALL provide an erasure console that streams the progress of the three erasure phases while an Erasure_Run executes.
6. THE Web_Console SHALL display a side-by-side comparison of a Blended_Artifact body before and after Surgical_Redaction.
7. THE Web_Console SHALL display an Erasure_Certificate together with the outcome of a live verification triggered from the interface.
8. THE Web_Console SHALL display per-Client retention status including Jurisdiction, retention interval, and counts of expiring Artifacts.
9. THE Web_Console SHALL require authentication for every route that reads memory content or triggers an Erasure_Run.
10. THE Web_Console SHALL expose an unauthenticated health route that reports no memory content.
11. THE Web_Console SHALL render every interactive control with a programmatically determinable name and SHALL support keyboard-only operation of the erasure console.
12. THE Web_Console SHALL serve a read-only demonstration mode that exposes seeded data for public evaluation without exposing mutation routes.
13. THE CLI SHALL expose the Web_Console as a `serve` verb.
14. THE Web_Console SHALL be hosted as a serverless function reached through an HTTPS function endpoint that the CDN_Distribution fronts as its single origin.
15. THE Web_Console SHALL provide a Memory_Tier view displaying every Memory_Tier together with the tables that Memory_Tier holds, the mutability of that Memory_Tier, the CockroachDB capability that Memory_Tier relies on, and the live row count of that Memory_Tier.

### Requirement 26: Command-Line Interface

**User Story:** As an operator, I want one coherent CLI, so that every capability is reachable without reading source code.

#### Acceptance Criteria

1. THE CLI SHALL be installed as the console entry point named `molt`.
2. THE CLI SHALL provide the verbs `erase`, `residue`, `attest verify`, `recall`, `watch`, `serve`, `seed`, `mcp`, `sensitivity`, and `contend`.
3. THE CLI SHALL exit with status code 0 on success and with a non-zero status code on failure for every verb.
4. THE CLI SHALL print machine-readable JSON when the operator passes a JSON output flag.
5. THE CLI SHALL read configuration from environment variables and from an optional configuration file, with environment variables taking precedence.
6. IF a required configuration value is absent, THEN THE CLI SHALL print the name of the missing value and SHALL exit with a non-zero status code.
7. THE CLI SHALL print no secret values in any output stream.

### Requirement 27: Cluster Provisioning and Least-Privilege Roles

**User Story:** As an operator, I want the cluster, roles, and accounts created by script, so that the deployment is reproducible and the privileges are minimal.

#### Acceptance Criteria

1. THE Provisioner SHALL create the CockroachDB_Cluster through the ccloud_CLI.
2. THE Provisioner SHALL apply every schema migration in order and SHALL record a migration's applied version in the CockroachDB_Cluster only after every statement of that migration has succeeded, including every statement applied under criterion 13, so that a recorded version means a fully applied file.
3. THE Provisioner SHALL create a writer role holding `INSERT` and `SELECT` privileges on the Ledger, Session, Derived_Artifact, Lineage_Edge, Client_Binding, and Embedding tables, together with `UPDATE` privilege confined to the columns named in Requirement 43 criterion 9 and Requirement 49 criterion 14 by the database-side guards those criteria specify.
4. THE Provisioner SHALL create an eraser role holding `SELECT`, `INSERT`, `UPDATE`, and `DELETE` privileges on the Derived_Artifact, Lineage_Edge, Client_Binding, and Embedding tables and `DELETE` privilege on the Ledger table, bounded by the privilege exclusions of Requirement 46 criterion 5.
5. THE Provisioner SHALL create a reader role holding `SELECT` privilege only.
6. THE Provisioner SHALL create one ccloud service account per role.
7. THE Provisioner SHALL store every generated credential in Parameter_Store and SHALL print no credential value to standard output.
8. THE Provisioner SHALL pull cluster audit logs through the ccloud_CLI for a caller-supplied time window.
9. THE Provisioner scripts SHALL be idempotent, so that a second execution against an existing cluster completes successfully and changes no state, including every statement applied outside a migration body under criterion 13.
10. THE development process SHALL use the Agent_Skills_Repo for schema review and query review, and THE Repository documentation SHALL record the reviews performed and the changes those reviews produced.
11. THE Provisioner SHALL verify through the ccloud_CLI that the CockroachDB Cloud control plane offers Managed_Backup listing and Managed_Backup configuration and offers no on-demand backup creation, and SHALL record that result in the same capability record that holds the results of the other capability probes.
12. THE Repository documentation SHALL record that the CockroachDB_Cluster offers no column-scoped `UPDATE` grant and no updatable view narrowed to writable columns, and that the column confinements of Requirement 43 criterion 9 and Requirement 49 criterion 14 are therefore enforced by database-side guards that refuse a statement changing a column the acting role may not change.
13. WHERE a schema migration holds a statement that the CockroachDB_Cluster serves only outside an explicit transaction, THE Provisioner SHALL apply that statement in an implicit transaction of its own after that migration's body has committed and SHALL require that statement to succeed.

### Requirement 28: Seed Data Generation

**User Story:** As a judge evaluating residue detection, I want realistic multi-client data containing genuine cross-client contamination, so that the demonstration finds real material rather than planted labels.

#### Acceptance Criteria

1. THE Seed_Generator SHALL create at least 3 Clients, at least 20 Sessions, and at least 2000 Events.
2. THE Seed_Generator SHALL create Sessions attributed to at least 3 distinct Agent_CLI names and at least 3 distinct machine identifiers.
3. THE Seed_Generator SHALL create at least 2 subagent Sessions with nesting depth of at least 2.
4. THE Seed_Generator SHALL create at least 3 Blended_Artifacts whose Client_Bindings name at least 2 Clients each.
5. THE Seed_Generator SHALL place at least 5 code fragments owned by one Client into Sessions scoped to a different Client, with no Client identifier, repository name, or file path revealing the owning Client.
6. THE Seed_Generator SHALL record the ground-truth mapping of each planted fragment to the owning Client in a file separate from the seeded memory content.
7. THE Seed_Generator SHALL produce Embeddings for every seeded Artifact carrying embeddable text.
8. THE CLI SHALL expose the generator as a `seed` verb accepting a deterministic random seed value.
9. WHEN the operator supplies the same random seed value, THE Seed_Generator SHALL produce content that is identical except for identifiers and timestamps.

### Requirement 29: Clean-Room Originality and Provenance Hygiene

**User Story:** As the author of this submission, I want the source to be demonstrably original and free of attributable metadata, so that the work stands on its own and carries no third-party text.

#### Acceptance Criteria

1. THE Repository SHALL contain only original source code, comments, docstrings, and documentation authored for Molt.
2. THE Repository SHALL list the `reference/` directory in `.gitignore`.
3. THE Repository SHALL contain no file under the `reference/` path in version control.
4. THE Repository SHALL contain no personal name, author name, email address, calendar date, clock time, timestamp literal, version history entry, or third-party project name in any source comment, docstring, or documentation file.
5. THE Repository SHALL contain comments and docstrings that describe behavior and intent only.
6. THE Memory_Store schema SHALL use `UUID` primary keys, a Client identifier column present in the first migration, `TIMESTAMPTZ` timestamp columns, and a `VECTOR` embedding column, as specified in Requirements 7, 9, and 10.
7. THE Memory_Store SHALL compute the Hash_Chain inside the writing transaction rather than by reading previously written state in a separate operation.
8. THE Repository SHALL include a check that scans tracked source and documentation files for the prohibited metadata patterns of criterion 4 and exits with a non-zero status code on any match.
9. THE Capture_Hook implementations for each Agent_CLI SHALL be derived from that tool's own public specification.

### Requirement 30: Security Posture

**User Story:** As a security owner, I want no credentials in source and no open endpoints, so that a memory system holding every client's code is not the weakest link.

#### Acceptance Criteria

1. THE Repository SHALL contain no credential, connection string, bearer token, or private key value.
2. THE Collector, the Policy_Watcher, the Web_Console, the Molt_MCP_Server, and the CLI SHALL retrieve the database connection string from Parameter_Store.
3. THE Collector SHALL retrieve the expected bearer token from Parameter_Store.
4. THE Molt components SHALL each connect to the CockroachDB_Cluster using the least-privileged role sufficient for that component's operations, as defined in Requirement 27.
5. THE Molt components SHALL require authentication on every network-exposed route other than the health routes specified in Requirements 5, 23, and 25.
6. THE Memory_Store SHALL pass every caller-supplied value to the CockroachDB_Cluster as a bound query parameter.
7. THE Molt components SHALL require TLS for every connection to the CockroachDB_Cluster.
8. THE S3_Bucket SHALL deny public access, SHALL require encryption at rest, and SHALL have Object Lock enabled at bucket creation, because Object Lock can be enabled on no existing bucket.
9. THE KMS signing key SHALL permit the sign operation only to the Certificate_Builder execution role.
10. THE Repository SHALL include a dependency manifest pinning every direct dependency to an exact version.
11. THE Repository SHALL contain a teardown script that removes every resource created for the delivered configuration, and THE teardown script SHALL complete without manual intervention, including removal of every Object Lock protected certificate version held under `GOVERNANCE` mode retention.
12. THE Molt components SHALL read every model provider credential from Parameter_Store or from an operator-provided file, and THE Repository SHALL contain no model provider credential value.

### Requirement 31: Observability

**User Story:** As an operator, I want metrics and structured logs, so that ingest volume, erasure activity, and failures are visible without reading the database.

#### Acceptance Criteria

1. THE Telemetry SHALL emit CloudWatch metrics for accepted Event count, rejected Event count, embedding call count, embedding failure count, recall query count, recall latency, Erasure_Run count, Erasure_Run duration, and certificate verification outcome count.
2. THE Telemetry SHALL emit every log record as a single-line JSON object containing a severity, a component name, a message, and a correlation identifier.
3. THE Telemetry SHALL include the Erasure_Run identifier as the correlation identifier on every log record produced during an Erasure_Run.
4. THE Telemetry SHALL exclude memory content bodies, credential values, and Embedding vectors from every log record.
5. THE Collector, the Policy_Watcher, and the Web_Console SHALL each expose a health route reporting component status and database reachability.
6. IF a CloudWatch call fails, THEN THE Telemetry SHALL write the log record to standard error and SHALL continue operation.

### Requirement 32: Resilience and Graceful Degradation

**User Story:** As an operator, I want each dependency failure to degrade one capability rather than the system, so that a model provider outage does not stop capture.

#### Acceptance Criteria

1. IF the configured Embedding_Provider is unavailable, THEN THE Memory_Store SHALL continue accepting Events and SHALL record affected Artifacts in the pending-embedding state.
2. WHEN the configured Embedding_Provider becomes available and pending-embedding Artifacts exist, THE Embedder SHALL produce the outstanding Embeddings in ascending creation order.
3. IF changefeeds are unavailable, THEN THE Policy_Watcher SHALL operate by polling as specified in Requirement 23.
4. IF the S3_Bucket is unavailable, THEN THE Certificate_Builder SHALL behave as specified in Requirement 21.
5. IF KMS is unavailable, THEN THE Certificate_Builder SHALL abort certificate creation, SHALL retain the Erasure_Run record, and SHALL report the signing failure.
6. THE Collector SHALL bound every database operation with a timeout of 10 seconds.
7. WHEN a Molt component receives a termination signal, THE component SHALL complete in-flight transactions and SHALL close database connections before exiting.

### Requirement 33: Scale and Cost Envelope

**User Story:** As an operator funding the demonstration myself, I want the system to hold many concurrent writers within a stated maximum monthly cost, so that the demonstration is affordable and still shows distribution.

#### Acceptance Criteria

1. THE Memory_Store SHALL accept concurrent Event writes from at least 20 distinct machine identifiers without a lock shared across unrelated Sessions.
2. THE Collector SHALL sustain an ingest rate of at least 100 Events per second.
3. THE Memory_Store SHALL keep total stored data at or below 10 GiB for the seeded demonstration corpus.
4. THE Molt components SHALL operate within a monthly budget of 50 million CockroachDB request units for the seeded demonstration workload.
5. THE Repository documentation SHALL record the measured storage footprint and the measured request-unit consumption of the seeded demonstration workload.
6. THE Repository documentation SHALL state a maximum monthly cost for the delivered demonstration configuration and SHALL contain a table naming each service used, that service's estimated monthly consumption, and that service's estimated monthly cost.
7. THE Embedder SHALL batch embedding requests to the configured Embedding_Provider in groups of at most 25 texts per call.
8. THE Molt components SHALL use no service whose demonstration cost is unbounded with respect to request volume.
9. THE Repository documentation SHALL record that CockroachDB_Cluster consumption is covered by introductory account credits rather than by a perpetual free tier, and SHALL record that Bedrock, Fargate, per-secret secret storage, and asymmetric key storage each carry no perpetual free tier.
10. THE deployment SHALL create no network address translation gateway and no interface endpoint.
11. THE Fargate tasks SHALL run in public subnets whose security groups permit no inbound traffic.
12. THE Molt components SHALL hold every configuration secret in a Parameter_Store tier carrying no per-parameter monthly charge.
13. THE Telemetry SHALL bound the count of distinct billable custom metric and dimension combinations by a configured maximum whose default value is 10.
14. WHERE emitting a metric would exceed the configured maximum count of distinct billable custom metric and dimension combinations, THE Telemetry SHALL emit that metric as a structured log record instead.

### Requirement 34: AWS Deployment Topology

**User Story:** As a judge, I want the application deployed on AWS with the specific services named, so that the platform requirement is satisfied verifiably.

#### Acceptance Criteria

1. THE Collector SHALL be deployed as a Lambda function fronted by an HTTPS function endpoint.
2. THE Web_Console SHALL be deployed as a Lambda function fronted by an HTTPS function endpoint that the CDN_Distribution fronts as its single origin, and the Policy_Watcher and the Molt_MCP_Server SHALL be deployed as Fargate services because each holds a long-running connection.
3. THE Embedder, the Adjudicator, and the Redaction_Rewriter SHALL invoke models through the configured providers of Requirement 37.
4. THE Certificate_Builder SHALL sign with KMS and SHALL store certificates in the S3_Bucket.
5. THE Molt components SHALL read secrets from Parameter_Store and SHALL emit telemetry to CloudWatch.
6. THE Repository SHALL contain infrastructure definitions under `infra/` that create every resource named in this requirement.
7. THE Repository documentation SHALL name each AWS service used and SHALL state what Molt does with that service.
8. THE Repository documentation SHALL name each CockroachDB tool used — the Managed_MCP_Server, distributed vector indexing, the ccloud_CLI, and the Agent_Skills_Repo — and SHALL state what Molt does with each tool.
9. THE deployment SHALL create no Application Load Balancer, because an Application Load Balancer HTTPS listener requires an ACM certificate that can be issued for no Application Load Balancer generated hostname, and because an Application Load Balancer carries an hourly charge.
10. THE deployment region SHALL be an AWS region in which every model required by the configured providers is available.
11. THE Provisioner SHALL verify that every required model identifier is reachable in the deployment region before deployment completes, and IF any required model identifier is unreachable, THEN THE Provisioner SHALL report the unreachable identifier and SHALL exit with a non-zero status code.
12. THE Repository documentation SHALL record the chosen deployment region and the verified model identifiers.

### Requirement 35: Repository and Submission Deliverables

**User Story:** As a judge, I want the repository to contain everything needed to set the project up and evaluate it, so that the submission is assessable without contacting the author.

#### Acceptance Criteria

1. THE Repository SHALL contain a `LICENSE` file holding the MIT licence text with the author's own copyright line.
2. THE Repository SHALL contain a `README.md` describing the problem, the architecture, the CockroachDB tools used, the AWS services used, and the demonstration URL.
3. THE Repository SHALL contain setup instructions that a reader can follow to provision the cluster, deploy the components, seed data, and run an Erasure_Run.
4. THE Repository SHALL contain an example configuration file listing every configuration key with a non-secret placeholder value.
5. THE Repository SHALL contain an architecture diagram under `docs/` showing capture, the CockroachDB memory core, the erasure engine, and the AWS services.
6. THE Repository SHALL organise source under `src/molt/`, tests under `tests/`, infrastructure under `infra/`, the web application under `web/`, operational scripts under `scripts/`, and documentation under `docs/`.
7. THE Repository SHALL declare Python 3.11 as the minimum supported runtime version, and THE Repository scripts SHALL invoke the Python 3.12 interpreter explicitly, because the interpreter the delivered machine offers by default is older than the declared minimum.
8. THE Repository SHALL declare `psycopg` as the CockroachDB driver and `boto3` as the AWS client library.
9. THE Repository documentation SHALL contain a demonstration script for a recording of at most 3 minutes that shows capture on two machines, semantic recall changing an agent decision, residue detection, an Erasure_Run, and certificate verification.
10. THE Repository documentation SHALL contain a traceability table mapping each hackathon judging criterion to the requirements that address that criterion.

### Requirement 36: Verification and Test Coverage

**User Story:** As the author, I want the hard claims backed by executable tests, so that correctness under concurrency and completeness of erasure are demonstrated rather than asserted.

#### Acceptance Criteria

1. THE Repository SHALL contain unit tests covering the Memory_Store data-access functions, the Event model serialisation, the Redactor, the Lineage_Graph queries, and the Certificate_Builder.
2. THE Repository SHALL contain integration tests executed against a running CockroachDB instance covering vector nearest-neighbour search, the recursive lineage common table expressions, Row-Level TTL expiry, changefeed consumption, and `AS OF SYSTEM TIME` reads.
3. THE Repository SHALL contain a concurrency test proving that concurrent Session counter updates lose no increments under SERIALIZABLE isolation.
4. THE Repository SHALL contain a concurrency test proving that an Artifact written concurrently with an in-flight Erasure_Run for the same Client either aborts with a serialization error or appears in that run's Dispositions.
5. THE Repository SHALL contain a comparison test demonstrating that a naive read-modify-write implementation of the same counter update loses increments, and THE comparison test SHALL assert that loss.
6. THE Repository SHALL contain a property-based test asserting erasure completeness: for generated memory graphs, after an Erasure_Run for a Client, no Artifact carrying a Client_Binding for that Client remains.
7. THE Repository SHALL contain a property-based test asserting erasure preservation: for generated memory graphs, after an Erasure_Run for a Client, every Artifact carrying no Client_Binding for that Client retains its original content digest.
8. THE Repository SHALL contain a property-based test asserting that the Redactor is idempotent.
9. THE Repository SHALL contain a property-based test asserting that Event serialisation followed by deserialisation reproduces an equivalent Event.
10. THE Repository SHALL contain a property-based test asserting that canonical certificate serialisation followed by parsing reproduces an equivalent certificate payload.
11. THE Repository SHALL contain a property-based test asserting that the Hash_Chain verification operation reports a mismatch whenever any stored row content is altered.
12. THE Repository SHALL contain an end-to-end test executing seed, capture, recall, erase, certify, and verify in sequence against a running CockroachDB instance.
13. THE Repository SHALL contain a test asserting that the Certificate_Verifier reports `signature_invalid` when any byte of a signed certificate payload is altered.
14. THE Repository SHALL contain a test asserting that the Residue_Detector recovers every planted cross-client fragment recorded in the Seed_Generator ground-truth mapping.
15. THE Repository SHALL contain integration tests using representative examples rather than generated inputs for model provider invocation, KMS signing, S3 storage, Parameter_Store retrieval, and CloudWatch emission.
16. THE Repository SHALL contain a test asserting that the metadata-hygiene check of Requirement 29 exits with a non-zero status code when a prohibited pattern is introduced.
17. THE Repository SHALL contain a test per Agent_Skill asserting that the skill definition parses and that the skill's declared entry point executes.
18. THE Repository SHALL contain a test asserting that the Molt_MCP_Server exposes no mutation tool.
19. THE Repository SHALL mark every test asserting a performance bound with the performance test marker, including a performance bound that needs no reachable CockroachDB instance, so that the CI_Workflow deselects that test.
20. THE Repository documentation SHALL record that the performance test marker gates on a reachable CockroachDB instance and that the gating is broader than the redaction latency bound of Requirement 4 criterion 7 needs.

### Requirement 37: Model Provider Abstraction

**User Story:** As an operator whose account holds no on-demand inference quota, I want model access behind an interface with several implementations, so that a provider restriction outside my control is a configuration change rather than a rewrite.

#### Acceptance Criteria

1. THE Repository SHALL define an Embedding_Provider interface accepting a text input and returning a float vector.
2. THE Repository SHALL define a Text_Provider interface accepting a prompt and returning generated text.
3. THE Repository SHALL contain a Bedrock implementation of the Embedding_Provider interface and a Bedrock implementation of the Text_Provider interface.
4. THE Repository SHALL contain one External_Embedding_Service implementation of the Embedding_Provider interface and one External_Text_Service implementation of the Text_Provider interface.
5. THE Provider_Selector SHALL select the Embedding_Provider implementation and the Text_Provider implementation from environment variables, so that switching provider requires a change to no source file.
6. THE Repository documentation SHALL name Bedrock as the default Embedding_Provider implementation and the default Text_Provider implementation, so that a restored inference quota requires a configuration change only.
7. THE delivered demonstration configuration SHALL select the External_Embedding_Service implementation and the External_Text_Service implementation.
8. THE Embedding_Provider implementations SHALL each return a vector of exactly 1024 dimensions, so that the fixed `VECTOR` column width of Requirement 10 is unchanged.
9. IF a configured Embedding_Provider implementation reports a vector width other than 1024 dimensions, THEN THE Provider_Selector SHALL reject the configuration, SHALL report the reported width and the required width, and SHALL exit with a non-zero status code before any Embedding is written.
10. THE default embedding model of the delivered demonstration configuration SHALL be a code-specialised retrieval model, because residue detection searches for semantically similar source code.
11. THE Provider_Selector SHALL read every provider credential from Parameter_Store or from an operator-provided file, and SHALL read no provider credential from source.
12. THE Provider_Selector, the Embedder, the Adjudicator, and the Redaction_Rewriter SHALL each write no provider credential value to any log record or output stream.
13. THE Repository documentation SHALL record which provider each of the Embedder, the Adjudicator, and the Redaction_Rewriter used in the delivered demonstration configuration.
14. THE Repository documentation SHALL state that the provider abstraction exists so that provider availability is a configuration concern rather than an architectural one.
15. THE Memory_Store SHALL store the provider name alongside the embedding model identifier on each Embedding row.
16. THE Repository documentation SHALL record that the Embedding_Provider implementation selected by the delivered demonstration configuration returns unit-normalised vectors and that the documented default Embedding_Provider implementation returns vectors that are not unit-normalised, so that the scaling of Requirement 10 criterion 10 is understood as load-bearing under a provider change rather than as a step that never runs.

### Requirement 38: Prompt Cache Efficiency

**User Story:** As an operator paying per token, I want adjudication prompts arranged so the provider can reuse the shared portion, so that one call per residue candidate does not cost one full prompt per candidate.

#### Acceptance Criteria

1. THE Adjudicator SHALL structure every prompt as a Stable_Prefix followed by the variable candidate excerpt, where the Stable_Prefix comprises the task instructions and the query Artifact excerpt.
2. THE Adjudicator SHALL produce a byte-identical Stable_Prefix for every candidate adjudicated against the same query Artifact within one Erasure_Run.
3. WHERE the configured Text_Provider supports prompt caching, THE Adjudicator SHALL mark the Cache_Boundary so that the provider treats the Stable_Prefix as cacheable content.
4. WHERE the configured Text_Provider supports no prompt caching, THE Adjudicator SHALL send the same prompt structure without a Cache_Boundary marker.
5. THE Telemetry SHALL record the cache-creation token count and the cache-read token count per adjudication batch.
6. THE Repository documentation SHALL record the measured cache hit ratio, the count of adjudication batches whose Stable_Prefix fell below the configured Minimum_Cacheable_Prefix_Length, and the resulting cost per Erasure_Run.
7. WHERE the Stable_Prefix length is at least the configured Minimum_Cacheable_Prefix_Length, THE Adjudicator SHALL mark the Cache_Boundary of criterion 3.
8. WHERE the Stable_Prefix length is below the configured Minimum_Cacheable_Prefix_Length, THE Adjudicator SHALL send the two-part prompt structure of criterion 1 with no Cache_Boundary marker, because a cache write carrying no subsequent cache read costs more than no caching.
9. THE configured Minimum_Cacheable_Prefix_Length SHALL have a default value of 16384 bytes, derived from the measured token floor of the delivered text model at the measured byte count per token.
10. THE configured prefix byte budget SHALL have a default value of 32768 bytes, exceeding the default Minimum_Cacheable_Prefix_Length by a factor of 2, so that an adjudication batch built to that budget caches and so that the Adjudicator judges each candidate against a longer query Artifact excerpt.
11. THE Telemetry SHALL record per Erasure_Run the count of adjudication batches whose Stable_Prefix length fell below the configured Minimum_Cacheable_Prefix_Length, so that the cache hit ratio of criterion 6 is interpretable rather than merely low.
12. THE Repository documentation SHALL record the measured minimum cacheable prefix length of the delivered text model and SHALL record that a Stable_Prefix below that length is deliberately left unmarked, because a cache write carrying no subsequent cache read costs more than no caching at all.
13. THE Repository documentation SHALL record that the delivered text model is retained rather than exchanged for a text model carrying a lower minimum cacheable prefix length, because the delivered text model's input token price is roughly one third of the alternative's, so exchanging models to enable a cost saving would raise cost.

### Requirement 39: Executable Agent Skills

**User Story:** As an agent operator using a different client, I want Molt's operational procedures shipped as loadable skills, so that verification and auditing are executable rather than described in prose.

#### Acceptance Criteria

1. THE Repository SHALL contain at least 3 Agent_Skills expressed in the open Agent Skills format.
2. THE Repository SHALL contain one Agent_Skill that verifies an Erasure_Certificate against a live CockroachDB_Cluster.
3. THE Repository SHALL contain one Agent_Skill that runs a residue sweep for a named Client.
4. THE Repository SHALL contain one Agent_Skill that audits retention status per Client.
5. THE Agent_Skills SHALL each declare the skill's inputs, the skill's outputs, and the skill's behavior.
6. THE Agent_Skills SHALL each invoke read-only operations only.
7. THE Agent_Skills SHALL be loadable by any MCP-compatible client without modification.
8. THE Repository documentation SHALL record that the review obligation of Requirement 27 criterion 10 stands alongside the Agent_Skills of this requirement.

### Requirement 40: Molt MCP Server

**User Story:** As an agent running in a client I did not write, I want Molt memory exposed as tools, so that fleet memory informs my decisions without embedding Molt in my own code.

#### Acceptance Criteria

1. THE Molt_MCP_Server SHALL expose a semantic recall tool over fleet memory.
2. THE Molt_MCP_Server SHALL expose a lineage ancestor retrieval tool and a lineage descendant retrieval tool.
3. THE Molt_MCP_Server SHALL expose a residue candidate search tool.
4. THE Molt_MCP_Server SHALL support stdio transport and HTTP transport.
5. THE Molt_MCP_Server SHALL connect to the CockroachDB_Cluster using a read-only database role.
6. THE Molt_MCP_Server SHALL expose no tool that mutates state in the CockroachDB_Cluster or the S3_Bucket.
7. THE Molt_MCP_Server SHALL derive the permitted Client set from server configuration rather than from tool arguments.
8. THE Molt_MCP_Server SHALL exclude from every tool result each Artifact whose Client_Bindings name only Clients absent from the permitted Client set.
9. THE Molt_MCP_Server SHALL record every tool invocation as an Event naming the tool, the redacted arguments, and the returned result count.
10. THE Molt_MCP_Server SHALL bound every tool result by a configured maximum result count whose default value is 50.
11. THE CLI SHALL expose the Molt_MCP_Server as an `mcp` verb.

### Requirement 41: Continuous Integration Workflow

**User Story:** As a reviewer, I want the checks and tests runnable by the repository host, so that the claims about hygiene and correctness are executed rather than asserted.

#### Acceptance Criteria

1. THE Repository SHALL contain a CI_Workflow definition that runs the strict static type check, the linter check, and the formatter check of Requirement 50, the metadata-hygiene check of Requirement 29, the unit test suite, and the property-based test suite.
2. IF any check or test invoked by the CI_Workflow fails, THEN THE CI_Workflow SHALL report a failing outcome.
3. THE CI_Workflow SHALL require no cloud provider credential and no CockroachDB_Cluster credential.
4. THE Repository documentation SHALL state that adding the CI_Workflow definition is a source change and is no repository publish step.
5. THE Repository SHALL contain a test asserting against the parsed structure of the CI_Workflow definition rather than against the definition text, because a structural assertion over a definition outlives a text match.
6. THE Repository SHALL declare the document parser that the test of criterion 5 uses in the dependency manifest of Requirement 30 criterion 10, pinned to an exact version.

### Requirement 42: Memory Tier Taxonomy

**User Story:** As a judge assessing whether CockroachDB is meaningfully the agent's memory layer, I want every kind of stored memory named as a tier alongside its tables and the database capability that tier relies on, so that the memory design is legible rather than implied.

#### Acceptance Criteria

1. THE Memory_Store SHALL classify every stored row into exactly one Memory_Tier drawn from `episodic`, `attribution`, `procedural_semantic`, `provenance`, `action`, and `working`.
2. THE Memory_Store SHALL assign the Ledger table to the `episodic` Memory_Tier, whose rows are append-only and hash-chained as specified in Requirements 7 and 8.
3. THE Memory_Store SHALL assign the Client_Binding table and the Attribution_Version history of that table to the `attribution` Memory_Tier, whose versioning is specified in Requirement 43.
4. THE Memory_Store SHALL assign the Derived_Artifact table to the `procedural_semantic` Memory_Tier, whose revisability is specified in Requirement 18 and whose confidence weighting is specified in Requirement 49.
5. THE Memory_Store SHALL assign the Lineage_Edge table, the Hash_Chain columns of the Ledger table, and the Ledger_Checkpoint table to the `provenance` Memory_Tier, whose signed checkpoints are specified in Requirement 45.
6. THE Memory_Store SHALL assign the Erasure_Lease table to the `action` Memory_Tier, whose fencing is specified in Requirement 44.
7. THE Memory_Store SHALL provide a Working_Memory table holding short-lived agent scratch state, keyed by Session identifier and scratch key, and SHALL assign that table to the `working` Memory_Tier.
8. THE Memory_Store SHALL store on each Working_Memory row a `JSONB` value column, a Client identifier, and an expiry timestamp column.
9. THE Retention_Manager SHALL configure CockroachDB Row-Level TTL on the Working_Memory table with a configured expiry interval whose default value is 3600 seconds, so that the CockroachDB_Cluster physically deletes each expired Working_Memory row.
10. THE Certificate_Builder SHALL derive every Erasure_Certificate field from tables outside the `working` Memory_Tier.
11. THE Certificate_Verifier SHALL execute every Verification_Query against tables outside the `working` Memory_Tier.
12. THE Memory_Store SHALL confine every reference to a Working_Memory row to the Working_Memory table, so that the Lineage_Edge, Client_Binding, Disposition, and Ledger_Checkpoint tables each reference no Working_Memory row.
13. WHEN the Erasure_Engine begins an Erasure_Run for a Client, THE Erasure_Engine SHALL delete every Working_Memory row carrying that Client identifier and SHALL record the deleted row count as one aggregate count rather than as per-row Dispositions.
14. THE Repository documentation SHALL contain a Memory_Tier table naming each Memory_Tier, the tables that Memory_Tier holds, the mutability of that Memory_Tier, and the CockroachDB capability that Memory_Tier relies on.
15. THE Repository documentation SHALL record that the `working` Memory_Tier is disposable and that expiry of that Memory_Tier is enforced by Row-Level TTL rather than by a scheduled process outside the CockroachDB_Cluster.
16. WHEN the Web_Console serves the Memory_Tier view of Requirement 25 criterion 15, THE Web_Console SHALL derive the live row count of each Memory_Tier from the CockroachDB_Cluster at request time rather than from a cached value or a precomputed value.
17. THE Web_Console Memory_Tier view SHALL display for the `working` Memory_Tier the count of Working_Memory rows whose expiry timestamp precedes the request time and the interval remaining until the next Row-Level TTL job run, so that expiry enforced by the CockroachDB_Cluster is observable rather than asserted.
18. WHEN the Web_Console serves the Memory_Tier view, THE Web_Console SHALL open no write transaction against the CockroachDB_Cluster.
19. WHILE the Web_Console serves the read-only demonstration mode of Requirement 25 criterion 12, THE Web_Console SHALL make the Memory_Tier view available.
20. THE Web_Console Memory_Tier view SHALL distinguish each Memory_Tier by a text label rather than by colour alone.
21. THE Interface_Specification SHALL describe the Memory_Tier view route together with the response shape of that route and the authentication requirement of that route.

### Requirement 43: Bitemporal Attribution History

**User Story:** As an auditor acting for a departing client, I want to know when the consultancy first attributed an artifact to my client and what changed that attribution since, so that the attribution record is a history rather than a current opinion.

#### Acceptance Criteria

1. THE Memory_Store SHALL store each Client_Binding as an Attribution_Version carrying a validity start timestamp, a validity end timestamp that is null while that version is current, and a superseding Attribution_Version identifier that is null while that version is current.
2. THE Memory_Store SHALL treat the detection method, the confidence value, the Artifact identifier, and the Client identifier of a stored Attribution_Version as immutable.
3. WHEN the Binding_Detector determines a detection method or a confidence value differing from the current Attribution_Version for a pair of Artifact identifier and Client identifier, THE Memory_Store SHALL close the current Attribution_Version by setting that version's validity end timestamp and superseding Attribution_Version identifier to the successor's generated identifier, SHALL then insert the successor Attribution_Version, and SHALL perform those two statements in that order in one transaction under the SERIALIZABLE isolation level.
4. THE Memory_Store SHALL expose an as-of-attribution query returning, for a named Artifact and a caller-supplied timestamp, every Attribution_Version whose validity interval contains that timestamp.
5. THE Memory_Store SHALL expose a current-attribution query returning only Attribution_Versions carrying a null superseding Attribution_Version identifier.
6. THE Erasure_Engine SHALL resolve the Client_Binding selection of Requirement 16 criterion 4 through the current-attribution query of criterion 5.
7. THE Erasure_Certificate SHALL contain, per touched Artifact, the validity start timestamp of the earliest Attribution_Version naming the erased Client and the detection method recorded on that Attribution_Version.
8. WHEN the Memory_Store supersedes an Attribution_Version, THE Memory_Store SHALL append one Event to the Ledger naming the Artifact identifier, the Client identifier, the superseded Attribution_Version identifier, and the superseding Attribution_Version identifier.
9. THE Provisioner SHALL confine every non-administrative role's `UPDATE` of a stored Attribution_Version to the validity end timestamp column and the superseding Attribution_Version identifier column, enforced by a database-side guard that refuses a statement changing the Artifact identifier column, the Client identifier column, the detection method column, or the confidence value column, and SHALL exempt the administrative path from that guard, because a database administrator can already drop the table.
10. THE Memory_Store SHALL answer the as-of-attribution query for an Artifact carrying at least 100 Attribution_Versions within 1 second.
11. THE Auditor_Gateway SHALL expose the as-of-attribution query of criterion 4 through the read-only view set of Requirement 24 criterion 5.
12. THE Memory_Store SHALL store the superseding Attribution_Version identifier as a value carrying no foreign-key reference to the Client_Binding table, so that the ordered statements of criterion 3 satisfy no self-referencing constraint mid-transaction.
13. THE Memory_Store SHALL maintain the integrity of the superseding Attribution_Version identifier through the single transaction of criterion 3 rather than through a database constraint, as Requirement 46 criterion 4 provides for the Disposition record's Artifact identifier.

### Requirement 44: Fenced Erasure Leases

**User Story:** As a governance owner, I want exactly one owner of an erasure for a client at a time and stale owners refused, so that a certificate cannot be produced by a worker that lost ownership.

#### Acceptance Criteria

1. THE Memory_Store SHALL store each Erasure_Lease with a Client identifier, an owner identifier, a Fencing_Generation, an acquisition timestamp, an expiry timestamp, and an idempotency key.
2. THE Memory_Store SHALL enforce a uniqueness constraint admitting at most one current Erasure_Lease per Client identifier.
3. WHEN the Lease_Manager grants an Erasure_Lease for a Client, THE Lease_Manager SHALL set that Erasure_Lease's Fencing_Generation to the highest Fencing_Generation previously recorded for that Client plus 1, and SHALL perform the grant under the SERIALIZABLE isolation level.
4. WHILE an Erasure_Lease for a Client is current, IF the Lease_Manager receives an acquisition request for that Client from a different owner, THEN THE Lease_Manager SHALL refuse the request and SHALL report the current owner identifier and the current Fencing_Generation.
5. WHEN the Lease_Manager renews a held Erasure_Lease, THE Lease_Manager SHALL extend that Erasure_Lease's expiry timestamp by a configured lease interval whose default value is 30 seconds.
6. WHERE an Erasure_Lease's expiry timestamp precedes the cluster's current timestamp, THE Lease_Manager SHALL grant takeover to a requesting owner through the ordered supersession of criterion 16 and SHALL increment the Fencing_Generation as specified in criterion 3.
7. THE Erasure_Engine SHALL carry the writing owner's Fencing_Generation on every Disposition write, every Erasure_Run completion record, and every Erasure_Certificate insert.
8. IF a write carries a Fencing_Generation other than the current Fencing_Generation for that write's Client, THEN THE Memory_Store SHALL refuse that write, SHALL report `stale_fencing_generation` together with the presented Fencing_Generation and the current Fencing_Generation, and SHALL persist no row from that write.
9. THE Erasure_Engine SHALL record one idempotency key per Erasure_Run.
10. WHEN the Erasure_Engine finalises an Erasure_Run whose idempotency key is already recorded as finalised, THE Erasure_Engine SHALL return the recorded finalisation result and SHALL perform no mutation.
11. THE Erasure_Certificate SHALL contain the Fencing_Generation of the owner that finalised the Erasure_Run.
12. IF the Erasure_Engine begins an Erasure_Run for a Client while holding no current Erasure_Lease for that Client, THEN THE Erasure_Engine SHALL abort the Erasure_Run before performing any mutation and SHALL report the current owner identifier.
13. THE Repository SHALL contain a demonstration in which at least 10 workers contend for one Client's Erasure_Lease, exactly one worker holds that Erasure_Lease, the holding worker is terminated, a second worker takes over after the expiry timestamp passes, the terminated worker resumes and attempts a disposition write, and that write is refused with `stale_fencing_generation`.
14. THE CLI SHALL expose the demonstration of criterion 13 as a `contend` verb that prints the winning owner identifier, the Fencing_Generation recorded at each takeover, and the refusal outcome.
15. THE Telemetry SHALL emit an `erasure.stale_generation_refused` metric for each write refused under criterion 8.
16. WHEN the Lease_Manager supersedes an Erasure_Lease, THE Lease_Manager SHALL close the current Erasure_Lease by setting that Erasure_Lease's supersession timestamp and superseding Erasure_Lease identifier to the successor's generated identifier, SHALL then insert the successor Erasure_Lease, and SHALL perform those two statements in that order in one transaction under the SERIALIZABLE isolation level.
17. THE Memory_Store SHALL store the superseding Erasure_Lease identifier as a value carrying no foreign-key reference to the Erasure_Lease table, so that the ordered statements of criterion 16 satisfy no self-referencing constraint mid-transaction.
18. THE Memory_Store SHALL maintain the integrity of the superseding Erasure_Lease identifier through the single transaction of criterion 16 rather than through a database constraint, as Requirement 46 criterion 4 provides for the Disposition record's Artifact identifier.

### Requirement 45: Signed Ledger Checkpoints

**User Story:** As an auditor, I want tamper evidence covering every session rather than only the sessions a certificate names, so that a consistently rewritten session that no certificate mentions is still detectable.

#### Acceptance Criteria

1. THE Checkpoint_Signer SHALL compute a Ledger_Checkpoint at a configured interval whose default value is 3600 seconds.
2. WHEN the Checkpoint_Signer computes a Ledger_Checkpoint, THE Checkpoint_Signer SHALL derive a root digest over the terminal Hash_Chain digest of every Session holding at least one Event within the checkpoint window.
3. THE Checkpoint_Signer SHALL compute the root digest with SHA-256 over the covered Session identifiers and terminal Hash_Chain digests ordered by Session identifier.
4. THE Checkpoint_Signer SHALL sign the root digest with the asymmetric KMS key of Requirement 21 criterion 12.
5. THE Memory_Store SHALL store each Ledger_Checkpoint with the window start timestamp, the window end timestamp, the covered Session count, the root digest, the signature, the KMS key identifier, and the signing algorithm name.
6. THE Checkpoint_Signer SHALL expose a checkpoint verification operation that recomputes a named Ledger_Checkpoint's root digest from live Ledger rows, verifies the stored signature against the public key retrieved from KMS, and reports agreement or disagreement.
7. WHERE the recomputed root digest differs from the stored root digest, THE Checkpoint_Signer SHALL report disagreement together with the identifier of every covered Session whose terminal Hash_Chain digest differs from the digest recorded at checkpoint time.
8. WHERE an Erasure_Run has deleted Ledger rows within a Ledger_Checkpoint's window, THE Checkpoint_Signer SHALL report disagreement together with the identifier of every Erasure_Run whose recorded Dispositions account for the deleted rows.
9. THE Provisioner SHALL grant no role `UPDATE` privilege or `DELETE` privilege on the Ledger_Checkpoint table.
10. THE Retention_Manager SHALL configure no Row-Level TTL on the Ledger_Checkpoint table.
11. THE Erasure_Certificate SHALL contain the identifier, the window bounds, and the root digest of the most recent Ledger_Checkpoint whose window end timestamp precedes `t_before`.
12. THE CLI SHALL expose the checkpoint verification operation of criterion 6 through the `attest verify` verb.
13. THE Repository documentation SHALL state that a Ledger_Checkpoint provides tamper evidence rather than tamper proofing.
14. THE Repository documentation SHALL state that a Ledger_Checkpoint extends integrity coverage beyond a principal holding database administrator privilege on the CockroachDB_Cluster, because the signature is produced by a key that the CockroachDB_Cluster holds no access to.
15. THE Repository documentation SHALL state that the per-Session Hash_Chain reaches an Erasure_Certificate only for Sessions that Erasure_Run touched, and that Ledger_Checkpoints cover every Session in the window.

### Requirement 46: Structural Protection of Audit Records

**User Story:** As a governance owner, I want the database itself to refuse deletions that would remove audit history, so that the audit trail survives an operator error rather than cascading away.

#### Acceptance Criteria

1. THE Memory_Store SHALL define every foreign key referencing an Erasure_Request row, an Erasure_Run row, a Disposition record, an Erasure_Certificate record, a Ledger_Checkpoint row, or a backup record with the referential action `ON DELETE RESTRICT`.
2. IF a statement attempts to delete a row referenced by a foreign key defined under criterion 1, THEN THE CockroachDB_Cluster SHALL refuse that statement and THE Memory_Store SHALL report the referencing table name and the referencing row count.
3. THE Memory_Store SHALL define the foreign keys of recomputable derived rows, comprising Embedding rows and Lineage_Edges, with the referential action `ON DELETE CASCADE`.
4. THE Memory_Store SHALL store the Artifact identifier on each Disposition record as a value carrying no foreign-key reference, so that a Disposition record survives the hard deletion of the Artifact that Disposition describes.
5. THE Provisioner SHALL grant the eraser role of Requirement 27 criterion 4 no `DELETE` privilege on the Erasure_Request table, the Erasure_Run table, the Disposition table, the Erasure_Certificate table, the Ledger_Checkpoint table, or the backup record table.
6. THE Repository SHALL contain a test asserting that deleting an Erasure_Run row referenced by a Disposition record is refused and that the Disposition record remains present.
7. THE Repository SHALL contain a test asserting that deleting a Derived_Artifact row deletes that Derived_Artifact's Embedding rows and Lineage_Edges by cascade.
8. THE Repository documentation SHALL record which tables are protected by `ON DELETE RESTRICT` and which tables cascade, together with the reason each table falls in its group.
9. THE Memory_Store SHALL store the superseding Attribution_Version identifier of Requirement 43 and the superseding Erasure_Lease identifier of Requirement 44 as values carrying no foreign-key reference, so that each supersession is performed as the ordered pair of statements those requirements specify and the writing transaction maintains each reference's integrity, as criterion 4 provides for the Disposition record's Artifact identifier and as Requirement 45 provides for a covered Session identifier.

### Requirement 47: Signed Ingress with Replay Resistance

**User Story:** As a security owner, I want ingest requests signed and time-bounded, so that a captured request body cannot be replayed into memory by a holder of the bearer token alone.

#### Acceptance Criteria

1. THE Collector SHALL require an Ingress_Signature on the Event batch endpoint of Requirement 5 criterion 1 and on the Session metadata endpoint of Requirement 5 criterion 2.
2. THE Ingress_Signature SHALL be an HMAC-SHA256 digest over the concatenation of the presented request timestamp and the request body, keyed by a shared secret that the Collector retrieves from Parameter_Store.
3. THE Collector SHALL read the request timestamp from a request header and SHALL read the Ingress_Signature from a request header.
4. IF the presented Ingress_Signature differs from the Ingress_Signature the Collector computes, THEN THE Collector SHALL respond with status code 401 and SHALL persist no record from that request.
5. IF the difference between the cluster's current timestamp and the presented request timestamp exceeds the configured maximum request age, THEN THE Collector SHALL respond with status code 401 and SHALL persist no record from that request.
6. THE configured maximum request age SHALL have a default value of 300 seconds.
7. IF the request timestamp header is absent from a request to an endpoint named in criterion 1, THEN THE Collector SHALL respond with status code 401 and SHALL persist no record from that request.
8. IF the Ingress_Signature header is absent from a request to an endpoint named in criterion 1, THEN THE Collector SHALL respond with status code 401 and SHALL persist no record from that request.
9. THE Collector SHALL compare the presented Ingress_Signature against the computed Ingress_Signature using a constant-time comparison.
10. THE Capture_Hook and the MCP_Proxy SHALL each retrieve the shared secret from configuration and SHALL present an Ingress_Signature on every request to an endpoint named in criterion 1.
11. THE Collector SHALL require the bearer token of Requirement 5 criterion 4 in addition to the Ingress_Signature on every endpoint named in criterion 1.
12. THE Collector SHALL authenticate every route other than the endpoints named in criterion 1 and the health endpoint of Requirement 5 criterion 3 by bearer token alone, so that an interactive caller holding no shared secret reaches the recall path of Requirement 13.
13. THE Telemetry SHALL emit a `collector.signature_rejected` metric for each request rejected under criterion 4, criterion 5, criterion 7, or criterion 8.
14. THE Repository documentation SHALL record that the bearer token resists no replay and that the Ingress_Signature bounds the window within which a captured request is replayable to the configured maximum request age.

### Requirement 48: Threshold Sensitivity Analysis

**User Story:** As an operator choosing residue thresholds, I want to see what each threshold pair would include before I commit to one, so that the single tuning decision that changes erasure scope is made with evidence.

#### Acceptance Criteria

1. THE Sensitivity_Analyzer SHALL accept a Client identifier and a Threshold_Grid comprising pairs of an auto-inclusion threshold and a review threshold.
2. THE Sensitivity_Analyzer SHALL evaluate the residue candidate set of Requirement 17 for every pair in the Threshold_Grid.
3. THE Sensitivity_Analyzer SHALL report per pair the auto-inclusion threshold, the review threshold, the candidate count, the count that pair would include without adjudication, and the count that pair would refer for adjudication.
4. WHERE the Seed_Generator ground-truth mapping of Requirement 28 criterion 6 is available, THE Sensitivity_Analyzer SHALL report per pair the count of planted cross-client fragments recovered.
5. THE Sensitivity_Analyzer SHALL perform no mutation of memory content.
6. THE Sensitivity_Analyzer SHALL invoke the configured Text_Provider for no candidate.
7. THE default Threshold_Grid SHALL comprise the auto-inclusion threshold values 0.10, 0.15, 0.20, 0.25, and 0.30 paired with the review threshold values 0.35, 0.40, 0.45, 0.50, and 0.55.
8. THE Sensitivity_Analyzer SHALL evaluate only pairs whose auto-inclusion threshold is at most that pair's review threshold, and SHALL report each excluded pair as inapplicable.
9. THE CLI SHALL expose the Sensitivity_Analyzer as a `sensitivity` verb that prints the result as a table and prints machine-readable JSON when the operator passes the JSON output flag of Requirement 26 criterion 4.
10. THE Web_Console SHALL display the Sensitivity_Analyzer result as a grid whose rows are auto-inclusion threshold values and whose columns are review threshold values.
11. THE Sensitivity_Analyzer SHALL complete an analysis over a 25-pair Threshold_Grid against a corpus of at least 100000 Embeddings within 120 seconds.
12. THE Repository documentation SHALL state that the Sensitivity_Analyzer calibrates thresholds and replays no recorded adjudication decision.

### Requirement 49: Confidence-Weighted Procedural Memory

**User Story:** As an agent relying on learned procedures, I want procedures to earn and lose standing from how the sessions that used them ended, so that memory improves with use rather than only accumulating.

#### Acceptance Criteria

1. THE Memory_Store SHALL store a Procedure_Confidence value in the closed interval from 0.0 to 1.0 on each Derived_Artifact of kind Learned_Procedure.
2. WHEN the Memory_Store writes a Derived_Artifact of kind Learned_Procedure, THE Memory_Store SHALL set that Derived_Artifact's Procedure_Confidence to a configured initial value whose default is 0.5.
3. WHEN the Recall_Engine returns a Learned_Procedure, THE Confidence_Tracker SHALL record a retrieval record naming the Learned_Procedure identifier, the consuming Session identifier, and the retrieval timestamp.
4. WHEN a Session that consumed a Learned_Procedure records an outcome classification, THE Confidence_Tracker SHALL record an outcome record naming the Learned_Procedure identifier, the Session identifier, and that outcome classification drawn from `succeeded`, `failed`, and `abandoned`.
5. WHEN the Confidence_Tracker records an outcome classification of `succeeded`, THE Confidence_Tracker SHALL increase that Learned_Procedure's Procedure_Confidence by a configured increment whose default value is 0.05, bounded above by 1.0.
6. WHEN the Confidence_Tracker records an outcome classification of `failed`, THE Confidence_Tracker SHALL decrease that Learned_Procedure's Procedure_Confidence by a configured decrement whose default value is 0.10, bounded below by 0.0.
7. WHEN the Confidence_Tracker records an outcome classification of `abandoned`, THE Confidence_Tracker SHALL leave that Learned_Procedure's Procedure_Confidence unchanged.
8. WHEN two recall results carry equal cosine distance, THE Recall_Engine SHALL order the result carrying the higher Procedure_Confidence first.
9. WHERE a Learned_Procedure's Procedure_Confidence is below a configured recall floor whose default value is 0.15, THE Recall_Engine SHALL exclude that Learned_Procedure from recall results.
10. THE Memory_Store SHALL retain every Learned_Procedure whose Procedure_Confidence is below the configured recall floor.
11. THE Erasure_Engine SHALL include every Learned_Procedure whose Procedure_Confidence is below the configured recall floor in the candidate set of Requirement 16.
12. WHEN the Confidence_Tracker changes a Procedure_Confidence, THE Confidence_Tracker SHALL append one change record naming the Learned_Procedure identifier, the prior value, the new value, the triggering outcome record identifier, and the change timestamp.
13. THE Confidence_Tracker SHALL write the changed Procedure_Confidence and the corresponding change record in one transaction under the SERIALIZABLE isolation level.
14. THE Provisioner SHALL confine the writer role's `UPDATE` of a Derived_Artifact row to the Procedure_Confidence column, enforced by a database-side guard that refuses a statement changing any other column of that row, and SHALL exempt the administrative path from that guard, because a database administrator can already drop the table.
15. THE Memory_Store SHALL expose a query returning the Procedure_Confidence change history for a named Learned_Procedure ordered by change timestamp.
16. THE Web_Console SHALL display for each Learned_Procedure the current Procedure_Confidence, the retrieval count, and the outcome counts per outcome classification.

### Requirement 50: Typed and Linted Codebase Gated in Continuous Integration

**User Story:** As a reviewer, I want type checking and style checking enforced by the workflow, so that the code's stated shapes are machine-verified rather than conventional.

#### Acceptance Criteria

1. THE Repository SHALL carry type annotations on every function parameter, every function return, and every module-level attribute in every module under `src/molt/`.
2. THE Repository SHALL pass a strict static type check over `src/molt/`, `tests/`, and `scripts/` reporting no error.
3. THE Repository SHALL record every type-check ignore directive in a documented allowlist naming the file path, the directive, and the reason for that directive.
4. IF a type-check ignore directive appears in a file that the documented allowlist names no directive for, THEN THE CI_Workflow SHALL report a failing outcome.
5. THE Repository SHALL pass a linter check and a formatter check over `src/molt/`, `tests/`, `scripts/`, and `infra/` reporting no violation.
6. THE CI_Workflow SHALL run the strict static type check, the linter check, and the formatter check before running the unit test suite and the property-based test suite.
7. THE strict static type check, the linter check, and the formatter check SHALL each require no cloud provider credential and no CockroachDB_Cluster credential.
8. THE Repository SHALL declare the type checker, the linter, and the formatter in the dependency manifest of Requirement 30 criterion 10, each pinned to an exact version.
9. THE Repository documentation SHALL record the commands that run the strict static type check, the linter check, and the formatter check on a developer machine.
10. THE Repository SHALL name every exception class with a name ending in `Error`, which the linter check of criterion 5 enforces.
11. THE Repository SHALL bind every shorter exception spelling that the Repository documentation uses as a module-level alias of that exception's suffixed class, so that either name resolves to one object.

### Requirement 51: Interface Specification and Glossary Deliverables

**User Story:** As a reviewer integrating with Molt, I want the interfaces, the vocabulary, and the threat posture written down as artifacts, so that evaluation and integration require reading no source code.

#### Acceptance Criteria

1. THE Repository SHALL contain an Interface_Specification in a machine-readable format describing every Collector route, every Web_Console route, and every Molt_MCP_Server tool.
2. THE Interface_Specification SHALL describe for each route and each tool the request shape, the response shape, the authentication requirement, and the error responses.
3. THE Repository SHALL contain the Interface_Specification as a tracked file under `docs/`.
4. THE Web_Console SHALL serve the Interface_Specification at a documented path that exposes no memory content.
5. THE Repository SHALL contain a glossary document under `docs/` defining every domain term, every system component name, and every external service name that the Repository documentation uses.
6. THE Repository SHALL contain a Threat_Model document under `docs/` recording every trust boundary of the delivered configuration.
7. THE Threat_Model SHALL record the threats credential compromise, Ledger tampering, concurrent erasure ownership, ingress replay, tenancy escape through a tool argument, prompt injection into an adjudication prompt, and provider credential leakage.
8. THE Threat_Model SHALL record for each threat named in criterion 7 the mitigation applied and the requirement that specifies that mitigation.
9. WHERE the delivered design applies no mitigation to a threat named in criterion 7, THE Threat_Model SHALL record that threat as accepted together with the reason for that acceptance.
10. THE Repository SHALL contain a test asserting that the Interface_Specification parses and that every route the Web_Console serves appears in the Interface_Specification.
11. THE Repository SHALL contain a test asserting that every tool the Molt_MCP_Server exposes appears in the Interface_Specification.

## Out of Scope

The following are explicitly excluded from this specification and from the submission:

1. Automatic discovery of Client ownership without configured markers or scope mapping.
2. Erasure of data held outside the CockroachDB_Cluster and the S3_Bucket, including agent vendor logs, model provider retention, and developer local filesystems.
3. Fine-tuned or self-hosted models. Every model call reaches a hosted provider through the abstraction of Requirement 37.
4. Multi-region or multi-cluster topologies.
5. A user management system, role editor, or single-sign-on integration for the Web_Console beyond the authentication of Requirement 25.
6. Real-time streaming of Events by any transport other than HTTP.
7. Support for Agent_CLI tools other than the five named in Requirement 1.
8. Historical provenance beyond the cluster garbage-collection horizon by any mechanism other than the append-only Ledger and recorded Dispositions.
9. Restoring from the pre-erasure backup. The backup exists as evidence, and restoration is an operator action outside Molt.
10. A billing or chargeback system built on the recorded cost Events.
11. Automated legal determination of Jurisdiction. Jurisdiction is operator-configured per Client.
12. Publishing the Repository, creating commits, or pushing to any remote until explicitly instructed.

## Correctness Properties

These properties are candidates for property-based testing and are traced to the acceptance criteria they verify. Each names the generated input space, the property, and the criteria covered.

### P1: Erasure completeness
**Generator:** random memory graphs of Sessions, Events, Derived_Artifacts, Lineage_Edges, and Client_Bindings over 2 to 5 Clients, with depth up to 4.
**Property:** after an Erasure_Run for Client C, the set of Artifacts carrying a Client_Binding for C is empty.
**Covers:** 16.2–16.6, 18.1, 18.4.

### P2: Erasure preservation
**Generator:** as P1.
**Property:** for every Artifact with no Client_Binding for the erased Client C before the run, the content digest after the run equals the content digest before the run.
**Covers:** 18.2, 18.4, 18.8.

### P3: Surgical redaction preserves other clients' bindings
**Generator:** Blended_Artifacts with 2 to 4 Client_Bindings.
**Property:** after Surgical_Redaction for Client C, the Client_Binding set equals the original set minus C, and the Artifact row still exists.
**Covers:** 18.2, 18.3, 18.4.

### P4: Lineage descendant closure
**Generator:** random directed acyclic Lineage_Graphs of up to 500 nodes.
**Property:** the descendant query result equals the transitive closure computed by an independent reference traversal.
**Covers:** 11.5, 11.7, 16.5.

### P5: Lineage graph acyclicity invariant
**Generator:** random sequences of Lineage_Edge insertions including edges that would close a cycle.
**Property:** after every accepted insertion the Lineage_Graph remains acyclic, and every cycle-closing insertion is rejected.
**Covers:** 11.4.

### P6: Hash chain tamper detection
**Generator:** random Event sequences of length 1 to 200 per Session, plus a random single-row mutation.
**Property:** chain verification reports no mismatch on the unmutated chain and reports the mutated row's sequence number on the mutated chain.
**Covers:** 8.2, 8.3, 8.6, 8.7, 22.7.

### P7: Hash chain uniqueness under concurrency
**Generator:** random interleavings of concurrent Event inserts into one Session.
**Property:** sequence numbers are unique and contiguous, and each row references exactly one predecessor digest.
**Covers:** 8.1, 8.5, 15.1.

### P8: Redaction idempotence
**Generator:** random nested payloads containing embedded secret-shaped strings.
**Property:** applying the Redactor twice equals applying the Redactor once.
**Covers:** 4.1, 4.4.

### P9: Redaction structure preservation
**Generator:** as P8.
**Property:** the redacted payload has the same key set, nesting shape, and value types as the input payload.
**Covers:** 4.4, 4.5.

### P10: Event round trip
**Generator:** random Events across all Event categories with arbitrary JSON-compatible payloads.
**Property:** deserialising the serialisation of an Event yields an equivalent Event.
**Covers:** 7.5, 7.7, 5.6.

### P11: Certificate canonical round trip
**Generator:** random Erasure_Certificate payloads.
**Property:** parsing the canonical serialisation yields an equivalent payload, and canonical serialisation is stable across key insertion orders.
**Covers:** 21.11, 21.12.

### P12: Signature verification detects any alteration
**Generator:** random signed certificates plus a random single-byte mutation of the payload.
**Property:** verification succeeds on the unmutated certificate and reports `signature_invalid` on every mutated certificate.
**Covers:** 22.2, 22.3.

### P13: Session depth invariant
**Generator:** random Session spawn trees.
**Property:** every Session's nesting depth equals its parent's nesting depth plus 1, and root Sessions have depth 0.
**Covers:** 9.3, 9.4.

### P14: Client binding uniqueness invariant
**Generator:** random repeated Client_Binding writes for the same Artifact and Client with varying confidence values.
**Property:** exactly one unsuperseded Attribution_Version exists per Artifact and Client pair, holding the maximum submitted confidence value.
**Covers:** 12.7, 43.5.

### P15: Binding inheritance monotonicity
**Generator:** random derivation chains.
**Property:** the Client_Binding Client set of a Derived_Artifact is a superset of the union of its parents' Client sets.
**Covers:** 12.3.

### P16: Recall tenancy filtering
**Generator:** random corpora with random permitted Client sets.
**Property:** every returned result carries at least one Client_Binding within the caller's permitted Client set.
**Covers:** 13.4.

### P17: Recall ordering
**Generator:** random query texts against a random corpus.
**Property:** returned cosine distances are non-decreasing, and the result count is at most k.
**Covers:** 13.1, 10.7.

### P18: Residue candidate disjointness
**Generator:** random corpora with explicit bindings and planted unlabelled fragments.
**Property:** the residue candidate set and the explicit sweep candidate set are disjoint, and their union contains every planted fragment above the auto-inclusion threshold.
**Covers:** 17.2, 17.3, 17.4.

### P19: Dry-run purity
**Generator:** random memory graphs.
**Property:** a dry-run Erasure_Run leaves every Artifact row, Embedding row, Lineage_Edge, and Client_Binding unchanged.
**Covers:** 18.11.

### P20: Erasure idempotence
**Generator:** random memory graphs.
**Property:** a second Erasure_Run for the same Client changes no rows and produces a certificate whose touched-Artifact list is empty.
**Covers:** 16.2–16.6, 18.1.

### P21: Retention expiry monotonicity
**Generator:** random Artifacts across Jurisdictions with random write timestamps.
**Property:** each Artifact's expiry timestamp equals its write timestamp plus its Jurisdiction's retention interval.
**Covers:** 14.3, 14.4.

### P22: Capture never fails the host agent
**Generator:** random hook payloads including malformed JSON, absent fields, oversized fields, and non-UTF-8 bytes.
**Property:** the Capture_Hook exits with status code 0 for every input.
**Covers:** 1.7, 6.6, 4.1.

### P23: Collector partial-batch acceptance
**Generator:** random batches mixing well-formed and malformed records.
**Property:** the persisted record count equals the well-formed record count, and the reported counts sum to the batch size.
**Covers:** 5.6.

### P24: MCP proxy transparency
**Generator:** random JSON-RPC message sequences.
**Property:** the byte sequence the proxy forwards equals the byte sequence the proxy received, excluding transport framing.
**Covers:** 2.4.

### P25: Policy evaluation confluence
**Generator:** random mutation streams and random Policy_Rule sets.
**Property:** the set of triggered Policy_Actions is independent of the order in which independent mutations are evaluated.
**Covers:** 23.4, 23.5.

### P26: Provider substitutability
**Generator:** random text inputs of length 1 to 8192 characters, paired with each configured Embedding_Provider implementation and with the non-unit-normalised Embedding_Provider stub of Requirement 10 criterion 14.
**Property:** every configured Embedding_Provider returns a vector of exactly 1024 dimensions, every Embedding the Embedder produces has an L2 norm equal to 1 within floating-point tolerance including every Embedding produced through the non-unit-normalised stub, and the schema and the nearest-neighbour query text are identical across provider selections.
**Covers:** 37.5, 37.8, 37.9, 10.2, 10.10, 10.14.

### P27: Backup path recording agreement
**Generator:** random Erasure_Runs under randomly chosen capability records marking the Self_Managed_Backup path available or unavailable.
**Property:** the recorded backup path value and the backup evidence in the Erasure_Certificate both name the path actually taken, and no run records a taken backup when no backup succeeded.
**Covers:** 19.2, 19.5, 19.6, 19.7, 21.7.

### P28: MCP server read-only tenancy
**Generator:** random corpora with random permitted Client sets and random tool invocations across every exposed tool.
**Property:** no invocation changes any row, and every returned Artifact carries at least one Client_Binding within the permitted Client set.
**Covers:** 40.6, 40.7, 40.8.

### P29: Collector request bound
**Generator:** random request bodies spanning sizes below, at, and above the configured maximum request body size.
**Property:** every request whose body exceeds the configured maximum is rejected with status code 413 and persists no record.
**Covers:** 5.10, 5.11.

### P30: Prompt cache prefix stability
**Generator:** random candidate sets of size 2 to 50 sharing one query Artifact, with random candidate excerpts, paired with Stable_Prefix lengths spanning below, at, and above the configured Minimum_Cacheable_Prefix_Length.
**Property:** the serialised Stable_Prefix is byte-identical across every candidate in the set, and a Cache_Boundary is marked at the end of that Stable_Prefix exactly when the Stable_Prefix length reaches the configured Minimum_Cacheable_Prefix_Length.
**Covers:** 38.1, 38.2, 38.3, 38.7, 38.8.

### P31: Fencing safety under contention
**Generator:** random interleavings of lease acquisition, renewal, expiry, takeover, worker termination, worker revival, and disposition writes across 2 to 20 workers contending for 1 to 3 Clients.
**Property:** at every point in the interleaving at most one Fencing_Generation is current per Client, and every write carrying a Fencing_Generation other than the current one for that Client is refused with `stale_fencing_generation` and persists no row.
**Covers:** 44.2, 44.3, 44.4, 44.6, 44.7, 44.8, 44.12.

### P32: Attribution history correctness
**Generator:** random sequences of 1 to 50 attribution writes per Artifact over 2 to 5 Clients, with random detection methods and confidence values, paired with random as-of timestamps spanning before, within, and after the write sequence.
**Property:** the as-of-attribution query returns exactly the Attribution_Versions whose validity interval contains the supplied timestamp, the current-attribution query returns exactly the Attribution_Versions carrying no superseding reference, and every Attribution_Version's detection method, confidence value, Artifact identifier, and Client identifier are unchanged from the values written.
**Covers:** 43.1, 43.2, 43.3, 43.4, 43.5.

### P33: Checkpoint verifiability
**Generator:** random Ledger states of 1 to 50 Sessions with 1 to 200 Events each, one computed Ledger_Checkpoint, and a random single-row mutation within the checkpoint window.
**Property:** checkpoint verification reports agreement before the mutation and reports disagreement naming the affected Session after the mutation.
**Covers:** 45.2, 45.3, 45.6, 45.7.

### P34: Ingress signature verification
**Generator:** random request bodies of 0 to 5 MiB paired with random timestamps inside and outside the configured maximum request age, and random alterations drawn from body mutation, signature mutation, timestamp-header removal, and signature-header removal.
**Property:** a correctly signed request whose timestamp falls within the configured maximum request age is accepted, and every altered body, altered signature, absent header, and out-of-window timestamp is rejected with status code 401 and persists no record.
**Covers:** 47.1, 47.2, 47.4, 47.5, 47.7, 47.8.

### P35: Threshold monotonicity and analysis purity
**Generator:** random corpora with explicit bindings and planted unlabelled fragments, paired with random Threshold_Grids whose pairs are ordered by each threshold.
**Property:** raising either the auto-inclusion threshold or the review threshold of a pair yields a candidate count no lower than the candidate count of the original pair, and every Artifact row, Embedding row, Lineage_Edge, and Client_Binding is unchanged after the analysis.
**Covers:** 48.2, 48.3, 48.5, 48.6, 48.8.

### P36: Procedure confidence bounds and direction
**Generator:** random sequences of 1 to 200 retrieval and outcome events per Learned_Procedure across the outcome classifications `succeeded`, `failed`, and `abandoned`.
**Property:** Procedure_Confidence remains within the closed interval from 0.0 to 1.0 after every event, moves upward on `succeeded`, moves downward on `failed`, stays equal on `abandoned`, and the count of change records equals the count of events that changed the value.
**Covers:** 49.1, 49.5, 49.6, 49.7, 49.12, 49.13.

### P37: Working memory disposability
**Generator:** random memory graphs paired with random Working_Memory rows across the same Clients, plus random deletions of those Working_Memory rows.
**Property:** every Erasure_Certificate field and every Verification_Query result is identical with and without the Working_Memory rows present, and no Lineage_Edge, Client_Binding, Disposition record, or Ledger_Checkpoint references a Working_Memory row.
**Covers:** 42.10, 42.11, 42.12.

### Deliberately not property-tested

The following are verified by integration tests with representative examples, because behavior does not vary meaningfully with generated input and the cost per iteration is high: model provider invocation, KMS signing, S3 Object Lock storage, Parameter_Store retrieval, CloudWatch emission, changefeed availability, ccloud_CLI provisioning and backup, Managed_MCP_Server connectivity, Agent_Skill loading, Web_Console route rendering, Row-Level TTL expiry of Working_Memory rows, the referential-action refusals of Requirement 46, the Interface_Specification serving route, and the type, linter, and formatter checks of Requirement 50.

## Traceability

### Hackathon hard requirements

| Hard requirement | Requirements |
|---|---|
| Agentic application using CockroachDB as persistent memory | 7, 8, 9, 10, 11, 12, 13, 14, 15, 40, 42 (Memory_Tier taxonomy), 43 (bitemporal attribution), 45 (signed checkpoints), 49 (confidence-weighted procedural memory) |
| Deployed on AWS | 34, 25 |
| At least two CockroachDB tools (five used) | 24 (Managed MCP Server), 10 (distributed vector indexing with `vector_l2_ops`), 19, 23.14 and 27 (ccloud CLI), 27.10 and 39 (Agent Skills), 23 (changefeeds) |
| At least one AWS service | 34, 30, 33. Model inference moved off Bedrock in the delivered configuration because on-demand inference quota is zero and non-adjustable on the delivered account; Bedrock remains the documented default provider under 37.3 and 37.6. The AWS service obligation is met by the remaining services: Lambda, Fargate, KMS, S3, Parameter Store, CloudWatch, and CloudFront. |
| Public repo with README, dependencies, example config, seed data, setup instructions, MIT licence | 35, 28, 30.10, 50.8, 51 (Interface_Specification, glossary, Threat_Model) |
| Functional public demo web app | 25, 34.2, 34.9, 48.10, 49.16, 51.4 |
| Video under three minutes showing the memory layer | 35.9 |
| Documentation of CockroachDB tools and AWS services used | 34.7, 34.8, 34.12, 37.13, 37.14, 42.14, 51.5 |
| Architecture diagram | 35.5 |

### Judging criteria

| Criterion | Requirements |
|---|---|
| Agentic Memory Design | 42 (explicit Memory_Tier taxonomy: episodic, attribution, procedural and semantic, provenance, action, working), 7, 9, 10, 11, 12, 13, 14, 25 (observable tier surface), 40, 43, 49 |
| Technical Implementation | 8, 15, 16, 17, 18, 20, 21, 22, 23, 36, 37, 43, 44 (fenced erasure leases), 45 (signed Ledger_Checkpoints), 46, 47 (signed ingress), 48, 50 (typing gated in CI) |
| Real-World Impact | 16, 17, 18, 21, 22, 24, 25, 39, 40, 43, 48, 51 |
| Production Readiness | 27, 30, 31, 32, 33, 34, 35, 36, 37, 38, 41, 44 (fencing), 45 (checkpoints), 46, 47 (ingress), 50 (typing and linting), 51 |
| Creativity & Originality | 17, 18, 21, 22, 23, 29, 37, 39, 42, 44, 45, 48 |
