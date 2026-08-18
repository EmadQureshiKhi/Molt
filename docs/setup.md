# Setup

Two paths through this document. The **local path** needs nothing but a checkout and an
interpreter, and it is what a reviewer should run first. The **deployed path** needs a
cloud account, a cluster, and provider credentials.

Every value in angle brackets is a placeholder. Nothing in this repository holds a
credential, a connection string, an account identifier, or a hostname, and nothing you
do while following this guide should put one in a tracked file. Secrets go to Parameter
Store or to a file outside the tree, and components read them from there.

Read the [status section of the README](../README.md#status) first. Every verb named below
exists in `src/molt/cli/verbs/`, so the steps that invoke `molt` are runnable against a
reachable instance, and the deployed path has been exercised end to end: eleven of twelve
stacks are running in a live account against a managed cluster, and a governed erasure has
completed there and produced a signed certificate that verifies. What the deployed path
below describes is therefore a repeat of something that has been done rather than a plan,
which is also why the failures it warns about are named — each one was met.

## Local path

### Tooling

```text
python3.12 -m pip install -r requirements.txt
```

The package is importable from `src/` with no install step, because the test
configuration puts it on the path. The workflow uses the same interpreter invocation, so
a check that passes here passes there.

For the tests and for `python3.12 -m molt.store.migrate` that is enough. For anything
that invokes the `molt` console entry point it is not:

```text
python3.12 -m pip install -e .
```

That matters beyond convenience on the deployed path. `scripts/provision_cluster.sh`
applies the migrations by calling the command-line interface — the binary it resolves is
`molt` unless `MOLT_CLI_BIN` names another — so a checkout where the entry point was
never installed fails that step on a missing command rather than on anything to do with
the cluster.

### A local instance

```text
scripts/run_local_db.sh start
```

The script starts a single-node insecure instance if one is not already running and
prints its connection string on standard output; diagnostics go to standard error, so
the output is safe to capture into a variable. It listens on the loopback interface and
carries no password. `status`, `stop`, and `wipe` do what they say, and every action is
idempotent.

### Both migration generations

```text
export MOLT_DSN="<connection string printed by run_local_db.sh>"
python3.12 -m molt.store.migrate
```

The runner applies every unrecorded migration in ascending order and records a version
only after all of that migration's statements have succeeded, so a second run applies
nothing. The connection string resolves through the configuration surface rather than
through an argument, which is why it is exported rather than passed.

Two generations land in one run:

| Generation | Contents |
|---|---|
| `001`–`007` | Core ledger and sessions, derived artifacts and lineage, embeddings and the vector index, erasure evidence, policy, row-level expiry, roles and privileges |
| `008`–`014` | Bitemporal attribution, erasure leases and fencing, signed checkpoints, the working tier, confidence-weighted procedural memory, structural protection of the audit record, and the grants those tables need |
| `015`–`022` | The structural diff summary, the referential actions an authorised erasure has to be able to cut, and the grants that running each path as its own narrow role exposed: two rounds of reader grants, the capability write the watcher performs before its loop, the procedural reads the disposition phase weighs a procedure by, the `DELETE` the erasure sweep finishes with, the two reads the change stream cannot be opened without, and the deletes a cascade expression reaches |

Twenty two files apply, and the last eight are worth naming individually because each one
exists to correct something the file before it could not.

`015_diff_summary.sql` adds the structural diff summary a surgical redaction leaves on
its disposition row, so the redaction comparison view is a query over stored evidence
rather than a re-read of a body that no longer exists.

`016_reader_grants.sql` grants `SELECT` on `erasure_candidate` and `run_session` to the
read-only role, and nothing further. Two read-only paths genuinely read those tables and
were never granted them: the residue walk, which reads the run's candidate set both to
take the material it adjudicates and to avoid recording a finding the explicit pass
already claimed, and the independent certificate verifier, which joins `run_session`
when it checks that a certificate's named checkpoint is accounted for in the deletion
case. Without the grant the sensitivity analyser and the tool server's residue tool
cannot run at all under the role they insist on, and every certificate naming a
checkpoint is unverifiable.

`017_erasure_references.sql` drops three referential constraints that stood between an
authorised erasure and the rows it was authorised to remove: a sub-agent session's
reference to the event that spawned it, the parent-session reference, and an event's
reference to the event it answers. Each formed a deletion cycle or crossed a batch
boundary that no ordering can satisfy. Every column and every index over it stays, so
lineage remains readable, and the derivation graph is carried independently in
`lineage_edge` in any case. `ledger.session_id` stays enforced — it forbids an orphan
event, it sits in no cycle, and the disposition phase now orders its hard deletes so
that every event is removed in an earlier batch than, or the same batch as, any session.

`018_console_reader_grants.sql` grants `SELECT` on `working_memory`, `erasure_request`,
`backup_record`, `approval_queue`, and `policy_rule` to the read-only role. It is the
same finding as `016` in two more places, and the reason it is a second file rather than
part of the first is worth reading once: the reader's grant list was drawn from the
read-only *components* that existed when each earlier file was written, and a table that
only a console view reads was in neither list. Narrowing the console's read-only views
onto the read-only connection is what turned that latent gap into a failure — the views
stopped borrowing a privilege nobody had granted them. Two views are affected, and both
are views a reviewer opens: the memory-tier view, which counts the erasure request and
the backup record among the action tier's tables and counts, expires, and reads back the
configuration of the working tier's; and the approval queue, whose listing reads the
queue joined to the rule that raised each entry. `SELECT` and nothing further on all
five, which matters most for the two where a wider grant would be tempting: the working
tier is the one tier whose rows are freely overwritten, and an approval is a record of a
human decision that the role rendering it must not be able to answer.

That file also carries a correction it cannot make where the claim is written. The prose
of `007` and `014` describes the read-only role as the role backing the auditor views. It
does not: `provision_roles.sh` grants `SELECT` on each auditor's views directly to that
auditor's own login and grants the read-only role to no auditor, deliberately, because
granting it to an untrusted third party would widen its reach to every client's rows
where the per-auditor grant reaches one client's four views. An applied migration is
never edited — the runner records a digest per file and refuses to run when one stops
matching — so the correction lands in the first later file to touch those grants.

`019_watcher_capability_grants.sql` grants `SELECT`, `INSERT`, and `UPDATE` on
`capability` to the watcher role. The watcher probes whether the cluster serves a
sinkless change stream and records the answer before it does any work, because that
answer decides which of its two paths it runs; the write is an upsert, so a fact
recorded again replaces its earlier answer rather than appending, and that one statement
needs both the insert and the update. `007` granted this table to the read-only role and
to the eraser and to no one else, so the watcher died on a missing privilege before
reaching its loop on every start. The gap only became visible when the watcher first ran
somewhere it connected as the role its privileges were written for: a run under an
administrative login writes the row and notices nothing. Nothing here reaches the ledger
or any tenant's rows — recording a platform fact about the cluster is not the ability to
record anything about a client.

`020_eraser_procedure_grants.sql` grants `SELECT` on `procedure_retrieval`,
`procedure_outcome`, and `procedure_confidence_change` to the eraser role. Deciding what
to do with a learned procedure consults its standing, and its standing is derived from how
often it was retrieved and how those sessions turned out, so the disposition phase reads
all three for the procedures belonging to the tenant being erased. `007` granted them to
the read-only role and to the writer and to the eraser not at all, so an erasure reached
its disposition phase and stopped there. It granted `SELECT` and nothing further, on the
reasoning that each of the three references its learned procedure with a cascading delete,
so removing the procedure removes them with it and no delete privilege is consulted. The
grants were right and that reasoning was wrong; `022` below is where the cluster corrected
it.

`021_sweep_and_stream_grants.sql` grants `DELETE` on `session` to the eraser role and
`SELECT` on `derived_artifact` and `lineage_edge` to the watcher role. Both were found by
putting each statement to the cluster as the role that issues it, inside a transaction
that was rolled back, rather than by reading a grant list: a catalogue scan tells you what
a role holds, not whether the statements a path issues are the statements the scan
recognised.

The sweep's delete is the end of the erasure sweep. `ledger` references `session` with no
action rather than with a cascade, so a session's events must be gone before the session
itself can be, and the sweep removes them in that order. The eraser held `SELECT` and
`UPDATE` on `session` and no `DELETE`, so it removed every event of a session and then
stopped on the row that session is. Nothing further guards it once the privilege is there:
the session guard is attached before update and a delete consults it not at all, and
`working_memory` references `session` with a cascade, so a deleted session's scratch rows
go with it.

The watcher's two reads are one mechanism. Its change stream is opened over `ledger` and
`derived_artifact`, and this platform serves a core changefeed only to a principal holding
`SELECT` on every table named in it; the watcher held the first and nothing at all on the
second. `lineage_edge` is the other half: a derived artifact row carries no session, so an
artifact mutation arriving on the stream is attributed by reading the earliest lineage edge
that names a session, or that names an event whose session answers for it. Granting the
stream without that read would move the failure one step later rather than remove it, which
is why both are in one file. This one is worth reading as a warning about quiet
degradation: the refusal was caught, recorded as a capability, counted as a metric, and
persisted as the mode, and the timestamp poll took over, so the watcher ran degraded on a
cluster that serves the stream perfectly well and all three records of that faithfully
said so to nobody.

One apparent gap of the same shape is not one, and it is recorded here because it will be
found again. The ingest path reads the capability record and the writer role is granted no
read of it. That is deliberate and stated where the read happens: the attempt is
best-effort, a failure is logged at debug and swallowed, and every accessor then reports
each platform fact as unprobed rather than as absent. An empty record is the honest
reading when the role that looked is not allowed to look, so the grant is not made.

`022_eraser_cascade_deletes.sql` grants `DELETE` on `procedure_retrieval`,
`procedure_outcome`, and `procedure_confidence_change` to the eraser role, and corrects what
`020` said about why they were not needed. A run reached its hard-delete step, removed the
artifacts it was authorised to remove, and stopped on this, in as many words: the privilege
is consulted *while building the cascade expression*. The deleting session must itself hold
the delete on every table a cascade will reach, so a cascading reference does not narrow what
a role needs — it widens it, to the transitive closure of the children of everything the role
deletes.

Which tables was derived rather than guessed. The erasure path deletes `ledger`, `session`,
`derived_artifact`, `embedding`, `lineage_edge`, `client_binding`, and `working_memory`; five
tables cascade from those, of which `lineage_edge` and `working_memory` were already
deletable by the eraser and these three were not. So the file closes the whole class rather
than the one table the run happened to name first, because the other two would have failed on
the next statement.

This is also the clearest case for why a correction is a new file. `020`'s grant list was
correct and its argument for stopping there was not, and an applied migration is never
edited, so the argument stands as written and the correction is made where the next reader
arrives. What generalises is the shape: a claim that the database will do something on a
role's behalf is worth checking against the database, because this one read plausibly and was
false.

The second generation is not a rewrite of the first. It amends it — replacing a total
uniqueness constraint with a partial one, re-creating referential actions as refusals
rather than cascades — so applying them in order matters and the runner enforces it.

### Suites

The credential-free suites, which is exactly what the workflow runs:

```text
python3.12 -m pytest tests/unit tests/property \
  -m "not integration and not services and not concurrency and not e2e and not perf"
```

With the local instance running, point the database-backed suites at it:

```text
export MOLT_TEST_DSN="<connection string printed by run_local_db.sh>"
python3.12 -m pytest tests/integration tests/concurrency
```

Suites marked `services` need cloud and model provider credentials and are the only
suites that reach outside the machine.

## Deployed path

Every resource the steps below create, and the four this design deliberately does not
create, are shown in [`assets/molt-deployment.svg`](../assets/molt-deployment.svg).

### Provision the cluster

```text
scripts/provision_cluster.sh --cluster <cluster-name> --plan basic \
  --region <region> --backup-target <bucket-url>
```

The script creates the cluster if it does not exist, applies every migration through the
migration runner, and records the platform facts the deployment branches on: whether the
distributed vector index was created and with which operator class, whether rangefeeds
and sinkless changefeeds are permitted, the measured garbage-collection horizon, and
whether the control plane offers an operation that creates a backup on demand. Every
step is idempotent, and a second run leaves the same record behind rather than appending
a second answer.

Those probe results are not decoration. Components read the capability record once at
process start and branch on it — no component branches on a cluster version string. See
[`architecture.md`](architecture.md#four-platform-facts-that-were-probed-not-assumed)
for what each fact implies.

### Roles and service accounts

```text
scripts/provision_roles.sh --cluster <cluster-name> --prefix <parameter-prefix> \
  --auditor <auditor-slug>:<client-slug>
```

Four least-privilege roles are created — the writer the Collector connects as, the
eraser the erasure path connects as, the reader the verifier, the analyser, and the tool
server connect as, and the watcher the policy watcher connects as — along with one
control-plane service account per role and the per-auditor read-only accounts and views.
The privileges themselves come from the migrations, which are the record of who may do
what; this script creates the roles those grants land on.

Credential handling has one rule: a generated credential goes from the generator
straight into Parameter Store and nowhere else. It is never printed, never passed as a
command-line argument, and never left behind in a file.

### What the account must already allow

Three account-level facts decide whether this deployment can be created at all, and each
was learned by being refused. None is a property of this repository, and none is
detectable from a checkout, so they are stated here rather than checked by a gate.

**The task service's linked role must exist.** Creating the task cluster fails with a
refusal to assume a role of that name if the account has never used the task service. One
command creates it, and it is a one-off per account:

```text
aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com
```

**A reservation of concurrency needs an allowance above the platform's floor.** A new
account's entire concurrency allowance equals the floor the platform insists remains
unreserved, so no function may reserve any of it, of any size. The ingest ceiling is
therefore expressible as the word `none`, which states no reservation rather than one of
zero — a reservation of zero throttles the function to nothing. What that gives up is in
the template's own description: ingest stays bounded by the account's allowance instead of
by its own value, and it can crowd out the console.

**A content distribution needs a verified account, and nothing else does.** Until the
provider verifies an account it refuses to create a distribution outright, and it refuses
anonymous requests to a *function's own* endpoint even where the endpoint is configured
for them and the function's resource policy allows them — the configuration is correct and
the account is not permitted to use it.

Neither restriction blocks a public deployment, because neither applies to a regional
endpoint. The `gateway` stack puts one in front of each function, and that is how this
deployment is reached: the console and the ingest function are both publicly addressable
without a distribution, without account verification, and without a support request. The
endpoints carry every method and path through unchanged, so the application receives the
request it would have received either way, and each is charged per request rather than per
hour — which is the same test the cost ceiling applies to everything else, and the reason a
load balancer is ruled out where this is not.

So the distribution is optional rather than blocking, and until the account is verified:

```text
infra/deploy.sh --skip cdn --region <region>
```

`--skip` may be given more than once and each omission is announced, because a stack that
was never created is a missing part of the deployment and a later stack resolving a value
from it will refuse rather than deploy something half-wired.

When verification does complete, the distribution deploys with no other change and takes
the regional endpoint as its origin rather than the function's own — `deploy.sh` resolves
that from the gateway stack's output. A custom domain then needs a certificate in
`us-east-1` and the alias declared on the distribution, and it may equally be attached to
the regional endpoint instead.

### Deploy the stacks, in order

Validate the parameter file first, then deploy:

```text
python3.12 scripts/validate_stack_params.py
infra/deploy.sh --params infra/params/<params-file>.json \
  --prefix <stack-prefix> --region <region>
```

Order inside the script is not cosmetic — each stack consumes names the earlier ones
publish. `STACK_ORDER` is:

`network` → `parameters` → `roles` → `kms` → `storage` → `collector` → `console` →
`gateway` → `cdn` → `watcher` → `mcp` → `observability`

Networking exists before anything that runs in it; the parameter names exist before
anything that reads them; the three roles exist before the key and the bucket, for the
reason below; the signing key and the bucket exist before the console stack, which
resolves the key identifier and the bucket name from their outputs; the distribution
exists after the console endpoint whose domain it is given; the tool server joins the
task cluster the watcher stack publishes. Nothing in the parameter file or in a stack
event is a secret: templates declare parameter *names*, and the provisioning scripts fill
the values.

**Why `roles` is a stack, and why it is third.** The key policy names the console
execution role as its one signing principal and as the single principal excepted from
its denial of signing, and the bucket policy names all three roles as the only
principals the bucket admits. The platform validates that a named principal exists at
the moment the resource carrying the policy is created, so a role created later is not
merely late — the key fails to create. Reordering alone does not settle it, because the
roles' own permissions name the key and the bucket: identity and resource point at each
other. The `roles` stack breaks that by creating each role with its trust policy and
only the permissions whose resource can be constructed from the account, the region, and
a parameter — parameter reads under the prefix, the parameter encryption key, the metric
namespace, and the log-stream writes whose group name is derived from the deployment
name. It resolves nothing from a later stack. The permissions
that do name a later resource are attached back to these roles by name by the stacks
that create those resources: `kms` attaches the signing permission to the console role
and the public-key read to the verifier, `storage` attaches the certificate-prefix
operations. `molt-verifier` is created here and nowhere else; it is an operator or
auditor identity rather than a service, so its trust policy names this account's
principals with a multi-factor session required, and it holds no signing permission
anywhere.

One permission of this stack is stated conditionally, and the reason is worth stating
because the alternatives are both wrong. The documented default provider needs
`bedrock:InvokeModel` on named models, and the platform refuses a role whose statement
names a resource that is neither well formed nor a wildcard — so a placeholder there is
not a value awaiting an operator, it is a stack that cannot be created. Writing a
wildcard instead would grant every model in the account to keep a statement the
delivered configuration never reaches, since the delivered selection is the external
provider for both provider roles. Deleting the statement would remove the default
path with nothing naming what to grant. Instead each model resource name defaults to
empty, the grant is stated only when a name is supplied, and the parameter file
supplies neither. So the delivered deployment holds no model permission at all, and
supplying `EmbeddingModelArn` or `TextModelArns` in the `roles` section restores the
grant with no template change.

Because `kms` and `storage` take role *names* rather than resource names, neither
resolves a cross-stack value for a role; the two function stacks take their role's
resource name from this stack's outputs and run under it rather than declaring a role of
their own.

`--dry-run` validates every stack's parameters and deploys nothing.

### The sequence that actually works

`deploy.sh` deploys all twelve stacks in one pass, and that is the right order *among the
stacks*. It is not the whole order, because two of the twelve need an artefact that does
not exist until application code has been packaged, and one of them fails outright if a
step that belongs after it is run before it. The real sequence is five steps, and the
script is invoked more than once. A stage is run by naming where it stops:
`deploy.sh --through STACK` deploys every stack up to and including the one named and
leaves the rest untouched, and `--dry-run` alongside it validates each stage's parameters
without creating anything.

| Step | What runs | Why here |
|---|---|---|
| 1 | `infra/deploy.sh --through storage` for `network`, `parameters`, `roles`, `kms`, `storage` | None of the five needs application code, and the parameter *names* must exist before anything writes values into them. `roles` precedes `kms` and `storage` because the key policy and the bucket policy name roles as principals and a named principal must exist when the resource carrying the policy is created; the roles' own permissions that name the key or the bucket are attached back to them by those two stacks |
| 2 | `scripts/provision_roles.sh` | Creates the four cluster roles and their service accounts, and overwrites the placeholder each connection-string parameter was declared with |
| 3 | Write the two provider credentials into the provider credential parameters | The embedding and text roles each read a credential by parameter name; neither is generated by a script |
| 4 | `scripts/package_functions.sh`, then `deploy.sh --through observability` for `collector`, `console`, `gateway`, `cdn`, `observability` | Both function stacks take a bucket and a key and require the object to exist at create time. Each also takes its execution role's resource name from the `roles` stack of step 1 rather than creating a role. `gateway` follows them because a permission naming a function that does not exist is refused, and it is what makes the deployment publicly reachable |
| 5 | Build and push the container image, then `deploy.sh` for `watcher` and `mcp` | The two Fargate services start tasks from an image, and a service with no image to pull never reaches a steady state |

**Step 2 cannot move before step 1.** A parameter in the `parameters` stack is a declared
resource, and creating one whose name is already taken fails. `provision_roles.sh` writes
the composed connection strings under those same names, so running it first leaves the
names occupied and the `parameters` stack fails on create rather than on update. The
script is written for the other order: it deletes a parameter that still holds the
declared placeholder before writing its own value, which is why running it *after* the
stack is created is the case it handles cleanly.

**Where the script's own order differs from this one.** `STACK_ORDER` itself carries no
stopping point; `--through` is where a stage's boundary is stated. Following the order end
to end in a single pass attempts `collector` and `console` before any archive exists in the
bucket and `watcher` and `mcp` before any image exists in the registry, and it never
invokes `provision_roles.sh` or writes a provider credential at all — nothing in the script enforces steps 2 and 3, or even knows about
them. Re-running the script is safe and is the means of applying a template change, so
the sequence above is expressed by invoking it repeatedly rather than by editing it. One
further divergence: `observability` sits last in `STACK_ORDER` and is grouped with step 4
above. It resolves no value from another stack, so its position is free either way; the
grouping is a convenience, not a dependency.

The mechanism for step 4 is `scripts/package_functions.sh`, which stages the application
package, archives it deterministically, uploads it, and prints the bucket and key the two
function stacks take. It can build the archive with the dependency tree or stage the
application package alone as a stub, and the stub is enough to create both functions,
both endpoints, both log groups, the checkpoint rule, and the distribution — the two
execution roles those functions run under exist already, from step 1 —
which is what makes a reachable demonstration address available before a full package is
produced. Read the usage block at the top of that script rather than trusting a flag
list here.

The mechanism for step 5 is a container image built from a `Dockerfile` and pushed to a
registry, whose location both task definitions take as `ImageUri`. Both service templates
already carry a `DesiredCount` parameter for exactly this ordering problem: a service
creation waits for steady state and a task cannot reach steady state before the image it
names exists, so a first deployment may bring the service up at zero and raise the count
once the image is in the registry rather than waiting out a rollback. **The `Dockerfile`
itself is not in the tree at the time of writing**, so nothing about how the image is
built is documented here. Check for it before following step 5; if it is absent, that step
is blocked rather than merely undocumented.

### Select the providers and load their credentials

Provider choice is configuration, not architecture. The selector reads the two role
selections and constructs the implementations, then refuses at startup any embedding
implementation whose reported width is not the schema's, so a misconfiguration fails
loudly at start rather than quietly at write time.

```text
export MOLT_EMBEDDING_PROVIDER="<implementation-name>"
export MOLT_TEXT_PROVIDER="<implementation-name>"
```

**What the registry actually holds, stated plainly, because "configuration not
architecture" is easy to read as a wider claim than the tree supports.** There are exactly
two registered implementations per role: `bedrock` and `external`. `bedrock` is the
documented default for both. The delivered selection is `external` for both, because the
account this was built against holds zero on-demand inference quota and that quota is not
adjustable — which is the situation the interface exists for, and the fix was a
configuration value rather than a rewrite. No third provider path exists: a name that is
not one of those two is refused against the registry keys rather than resolved.

Three settings carry no default and must be supplied, because a default naming a model
would be a choice made on the operator's behalf:

```text
export MOLT_EMBEDDING_MODEL_ID="<embedding model>"
export MOLT_ADJUDICATION_MODEL_ID="<text model the Adjudicator calls>"
export MOLT_REWRITE_MODEL_ID="<text model the Redaction_Rewriter calls>"
```

They are three rather than two because one text provider serves two roles that may name
different models; where only one of the two text identifiers is set, the other stands in.
A missing identifier is reported as missing configuration naming the key, not as a
provider failure.

The embedding role has one further gate. The selector probes the embedding
implementation at startup and refuses any reported width other than the schema's 1024
dimensions, falling back to the width the implementation declares when the probe cannot be
answered — because the declared width is what every later call would produce. The refusal
names both widths and the two settings to change, and it happens before a single vector is
written rather than one insert at a time after a run has begun.

Credentials come from exactly two places, and each role can use either:

| Source | Setting |
|---|---|
| Parameter Store | `MOLT_EMBEDDING_CREDENTIAL_PARAM`, `MOLT_TEXT_CREDENTIAL_PARAM` — the parameter *name*, not the value |
| A file outside the tree | `MOLT_EMBEDDING_CREDENTIAL_FILE`, `MOLT_TEXT_CREDENTIAL_FILE` — a path readable only by the operator |

A loaded credential is held in a wrapper whose string forms render a fixed placeholder,
so it cannot reach a log record, an exception message, or an output stream. Use
`config.example.toml` as the reference for every configuration key; it carries
placeholders and no secret defaults.

### Register the per-tool hooks

Each supported agent tool has its own hook specification: its own event names, its own
field names, its own way of receiving injected context, and its own convention for a
blocking decision. Those differences are recorded per tool in [`hooks/`](hooks), and
each note also records the capability flags that follow from what the tool actually
offers. Register the hooks as each note describes.

Every hook invocation needs three things, and none of them is a database credential:

```text
export MOLT_COLLECTOR_URL="<collector endpoint>"
export MOLT_COLLECTOR_TOKEN_PARAM="<parameter name holding the bearer token>"
export MOLT_INGRESS_SECRET_PARAM="<parameter name holding the shared signing secret>"
export MOLT_CLIENT_MAP="<path to the workspace-to-client mapping>"
```

The hook computes a keyed digest over the request timestamp and the request body with
the shared secret, and sends it as a header beside the bearer token. The Collector
compares it in constant time and then checks the request age against
`MOLT_INGRESS_MAX_AGE_SECONDS`. A mismatch, a stale timestamp, or an absent header
rejects the batch and persists nothing. The bearer token alone resists no replay; the
signature is what bounds the replayable window.

Confirm registration by inspecting the ledger for the session the tool just ran, rather
than by trusting the hook's exit status — capture exits successfully in every branch on
purpose, so that a capture failure never breaks an agent run.

### Seed data

`--seed` is required; the rest carry defaults.

```text
molt seed --seed <integer> --clients <n> --sessions <n> --events <n> \
  --ground-truth <path outside the tree>
```

Seeding is deterministic in the given seed, and it deliberately plants cross-client
contamination — fragments of one client's code inside sessions attributed to another —
because that is the only honest way to test residue detection. The ground-truth mapping
of what was planted where goes to a **separate file**, never into the cluster, so
residue detection cannot accidentally read the answer key.

### Run an erasure under a lease

`--client`, `--requester`, and `--justification` are all required, so an unattributed
erasure is a usage error rather than a run.

```text
molt erase --client <client-slug> --requester <requester-id> \
  --justification "<why this erasure is authorised>" --dry-run
```

Start with `--dry-run`, which mutates no memory content and prints the disposition
summary the real run would produce. Then drop it.

The run acquires a lease before it touches anything. If another worker holds the current
lease, the run aborts before any mutation and names the current owner and generation
rather than proceeding hopefully. Once granted, the lease is renewed while the run is in
flight, and every evidence write carries the generation the worker believes it holds, so
a superseded worker's write is refused by the database.

Two other things happen before the first mutation: the client's working rows are deleted
as one set-based statement with an aggregate count recorded, and a pre-erasure backup is
taken. A backup failure aborts the run.

To inspect without erasing, `molt residue --client <client-slug>` prints candidates with
their distances, bands, and decisions and mutates nothing. To choose thresholds against
evidence rather than intuition, `molt sensitivity --client <client-slug>` prints the
consequence of each threshold pair over one retained candidate set.

### Compute a checkpoint

Checkpoint computation runs on a configured interval from the scheduled invocation of
the console function, which is the only principal holding the signing permission. It
gathers the terminal chain digest of every session holding at least one event inside the
window, computes a root digest over them in a fixed order with fixed separators, signs
it with the same key the certificate path uses, and stores the window bounds, the
covered session count, the root digest, and the signature.

So there is no operator verb that *creates* a checkpoint. There is one that checks it:

```text
molt attest verify --checkpoint <checkpoint-id>
```

That verb names exactly one of `--certificate`, `--s3-key`, or `--checkpoint`, and naming
none or more than one is a usage error rather than a defaulted choice. It takes no
positional path. On disagreement, verification names every
covered session whose terminal digest now differs from the digest recorded at checkpoint
time and partitions the changes: a difference explained by the dispositions of a
governed erasure run is reported as accounted, and anything else is the finding. That
partition is the whole point — without it, every lawful erasure would look like
tampering.

What a checkpoint buys over the per-session chain is coverage of every session in a
window rather than only the sessions a certificate names, and detection by a party who
does not trust the cluster administrator, because the signing key lives outside the
cluster. What it does not buy is prevention. This is tamper evidence, not tamper
proofing.

### Run the MCP server

```text
molt mcp --transport stdio --client <client-slug> --max-results <n>
```

The permitted client set is resolved from these arguments and the configuration file at
startup and never from a tool argument, which is what closes the tenancy-escape path:
there is no schema field an argument could use to ask about another client. Every
exposed tool is read-only, the database role holds `SELECT` only, and the tenancy filter
is applied inside SQL.

`--transport http --bind <host>:<port>` runs the same registry over the HTTP transport.

## The last steps, always

Run the four static checks, then the hygiene gate. In that order, and last, so that a
tree that passes is a tree with no unformatted file, no untyped definition, and no
attributable metadata in it.

```text
python3.12 -m mypy
python3.12 scripts/check_type_ignores.py
python3.12 -m ruff check src/molt tests scripts infra
python3.12 -m ruff format --check src/molt tests scripts infra
python3.12 scripts/hygiene.py
```

The hygiene gate exits 0 with a per-class scanned-file count, 1 with one line per
finding, and 2 when one of its list files is malformed — so a broken configuration is
never reported as a clean scan. See [`hygiene.md`](hygiene.md) for what the eight
pattern classes are and [`typing.md`](typing.md) for why the order is fixed.
