# Setup

Two paths through this document. The **local path** needs nothing but a checkout and an
interpreter, and it is what a reviewer should run first. The **deployed path** needs a
cloud account, a cluster, and provider credentials.

Every value in angle brackets is a placeholder. Nothing in this repository holds a
credential, a connection string, an account identifier, or a hostname, and nothing you
do while following this guide should put one in a tracked file. Secrets go to Parameter
Store or to a file outside the tree, and components read them from there.

Read the [status section of the README](../README.md#status) first. Several steps below
depend on the command-line interface, which is not yet built, and each such step says so.

## Local path

### Tooling

```text
python3.12 -m pip install -r requirements.txt
```

The package is importable from `src/` with no install step, because the test
configuration puts it on the path. The workflow uses the same interpreter invocation, so
a check that passes here passes there.

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

A later migration in the tree adds the structural diff summary a surgical redaction
leaves on its disposition row, so the redaction comparison view is a query over stored
evidence rather than a re-read of a body that no longer exists.

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

### Deploy the stacks, in order

Validate the parameter file first, then deploy:

```text
python3.12 scripts/validate_stack_params.py
infra/deploy.sh --params infra/params/<params-file>.json \
  --prefix <stack-prefix> --region <region>
```

Order is not cosmetic — each stack consumes names the earlier ones publish:

`network` → `parameters` → `kms` → `storage` → `collector` → `console` → `cdn` →
`watcher` → `mcp` → `observability`

Networking exists before anything that runs in it; the parameter names exist before
anything that reads them; the signing key exists before the bucket policy that trusts
it; the bucket exists before the console that writes certificates into it; the
distribution exists after the function endpoint it fronts. Nothing in the parameter file
or in a stack event is a secret: templates declare parameter *names*, and the
provisioning scripts fill the values.

`--dry-run` renders what would be deployed without deploying it.

### Select the providers and load their credentials

Provider choice is configuration, not architecture. The selector reads the two role
selections and constructs the implementations, then refuses at startup any embedding
implementation whose reported width is not the schema's, so a misconfiguration fails
loudly at start rather than quietly at write time.

```text
export MOLT_EMBEDDING_PROVIDER="<implementation-name>"
export MOLT_TEXT_PROVIDER="<implementation-name>"
```

Both default to the documented default provider. Credentials come from exactly two
places, and each role can use either:

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

*Needs the command-line interface, which is not yet built.*

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

*Needs the command-line interface and the engine orchestration, neither of which is
finished. What follows is the intended shape.*

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

*Also needs the command-line interface.* On disagreement, verification names every
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

*Needs the command-line interface and the tool server, neither of which is finished.*

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
