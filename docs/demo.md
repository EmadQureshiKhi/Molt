# Demonstration recording script

Three minutes is the whole constraint, and it decides the structure rather than
trimming it. Five things have to be shown — capture on two machines, semantic recall
changing an agent decision, residue detection, an Erasure_Run, and certificate
verification — and each of them is only convincing if the recording shows the
*evidence* rather than a claim about it. So every beat below names what is on screen,
what is said over it, and the exact command or URL that produces it, and each beat is
budgeted in seconds against a running total that lands under the limit.

Every command in this script is a verb the command-line interface actually has, taken
from `src/molt/cli/` and from [`setup.md`](setup.md). Nothing here invents a flag.

## The deployed console, for a reviewer who is not recording anything

The console is live, and a reviewer who only wants to look needs no checkout and no script.
The README's [live demo](../README.md#live-demo) section carries the address, the one
credential, and a short tour in the order the system works.

It is a working console rather than a read-only one, which is a deliberate change from how
it was first deployed. Read-only mode refused every route that would change stored memory,
which meant the two beats that matter most -- an Erasure_Run and the certificate it produces
-- were the two a visitor could not see, and a system whose whole claim is provable
forgetting was asking to be taken on trust. So the mode is off, anonymous access is refused
in its place, and a reviewer signs in with the published credential and can start a run.

A dry run is the one to start first and is what the console is shaped for: it performs the
entire analysis, records a disposition for every artifact it found, mutates no memory
content, and finishes inside the time a function is allowed to live. A live run's surgical
redaction is one model call per blended artifact and does not fit in that budget, so a live
run is performed as the console's execution role from somewhere with no request timeout --
which is what the rest of this script covers. The certificate such a run issues is served
and verified through the console like any other.

## Placeholders

Every token in angle brackets is substituted before recording. Nothing in this
repository holds a hostname, a connection string, or a credential, so none of these
has a value here.

| Token | What to substitute | Where it comes from |
|---|---|---|
| `<CONSOLE-ORIGIN>` | The console's base URL, with no trailing slash | The `gateway` stack's `ConsoleEndpoint` output, which is the address the README names. For a local recording, the bind `molt serve` prints |
| `<DSN>` | The cluster connection string | `scripts/run_local_db.sh start` locally, or the Parameter_Store parameter in a deployment. Never on screen |
| `<SEED>` | The integer the corpus is generated from | Chosen by the recorder. The same value reproduces the same corpus |
| `<GROUND-TRUTH-PATH>` | A file path **outside** the checkout | Chosen by the recorder. It holds the answer key for what was planted where |
| `<CLIENT-SLUG>` | The tenant being erased | One of the seeded slugs: `veltrine`, `orbanic`, `quillstone`, `mirebrook` |
| `<OTHER-SLUG>` | A tenant that is *not* erased, whose memory must survive | Any other seeded slug |
| `<QUERY>` | The recall query text | Chosen by the recorder. A phrase from the seeded domain vocabulary, such as a duplicate-key rejection in a scheduler |
| `<REQUESTER-ID>` | The principal authorising the erasure | The recorder's own governance identity |
| `<JUSTIFICATION>` | One sentence naming why the erasure is authorised | The recorder's own words |
| `<RUN-ID>` | The Erasure_Run identifier | Printed by `molt erase` as `run_id` |
| `<CERTIFICATE-KEY>` | The certificate's object key | Printed by `molt erase` as `certificate_object_key` |
| `<SESSION-ID>` | A Session identifier from the fleet overview | Printed by `molt recall` as `session_id`, or read off the console |

## Before the camera rolls

None of this is in the running time. It is the state the first beat opens on.

```text
scripts/run_local_db.sh start
export MOLT_DSN="<DSN>"
molt migrate
molt seed --seed <SEED> --clients 4 --sessions 28 --events 2600 \
  --ground-truth <GROUND-TRUTH-PATH>
molt serve
```

Two things about that seeding call. Its volumes are the design's own defaults, which
is what makes the corpus large enough for the residue beat to have something to find:
it plants cross-client contamination deliberately, and the mapping of what went where
goes to a file outside the tree so that residue detection cannot read its own answer
key. And it is deterministic in `<SEED>`, so a retake produces the same corpus and
the same distances.

**Which beats need the deployed stacks.** Beats one through four need nothing but the
local instance. Beats five and six need the `kms` and `storage` stacks, because a
certificate is signed with the asymmetric key and written into the Object Lock bucket;
a local-only recording can show the run and its dispositions but has no signed
document to verify. Deploy in the order [`setup.md`](setup.md) states.

**Demonstration mode refuses the erasure beat from the console.** A deployment with
`MOLT_DEMO_MODE` set answers `403` on `POST /erase` from a route-name denylist before
any handler runs, which is the containment the console is meant to have. So beat five
drives the run from the command line and shows the console's *read* views of the same
run. `molt serve --demo` reproduces that posture locally if the recorder wants to show
the refusal itself.

## The shot list

| # | Beat | Seconds | Elapsed |
|---|---|---|---|
| 1 | Cold open: what is on the screen | 12 | 12 |
| 2 | Capture on two machines | 30 | 42 |
| 3 | Semantic recall changes an agent decision | 32 | 74 |
| 4 | Residue detection finds what no identifier names | 30 | 104 |
| 5 | An Erasure_Run under a lease | 40 | 144 |
| 6 | Certificate verification by an independent reader | 28 | 172 |
| 7 | Close | 6 | 178 |

One hundred seventy-eight seconds, which leaves two under the limit for the recorder's
own pacing rather than for a sixth idea.

### Beat 1 — Cold open (12 s)

**On screen.** The fleet overview, `<CONSOLE-ORIGIN>/`, already loaded. Seeded
Sessions with their tenants, machines, and outcomes.

**Said.** "This is every run four coding agents made across four client engagements —
captured, attributed, and erasable. Nothing here was typed in by hand; it was
observed."

**Command or URL.** `<CONSOLE-ORIGIN>/`

Open on the evidence rather than on a title card. The one sentence names the three
things the remaining beats prove in order: it was captured, it is attributed, and it
can be removed.

### Beat 2 — Capture on two machines (30 s)

**On screen.** A terminal running one recall, with the `machine_id` field of two
results visible side by side.

**Said.** "Capture is per machine and provenance is per row. These two results came
off different machines and the same client's memory holds both. The Session
identifier, the machine identifier, the instant, and the outcome on each result are
read back out of the stored Session row, not composed by the reader."

**Command.**

```text
molt recall "<QUERY>" --client <CLIENT-SLUG> -k 5 --json
```

**How to show two machines honestly.** The Seed_Generator cycles four machine
identifiers — `workstation-a1`, `workstation-b2`, `builder-c3`, `builder-d4` — across
the Sessions it writes, one per Session in order, so a corpus of 28 Sessions carries
all four and any recall page spanning more than a few Sessions carries at least two.
Point the cursor at two differing `machine_id` values on the page. That is a real
two-machine corpus in the cluster, and the script says exactly that rather than
implying two laptops were filmed.

If the recorder wants a genuinely live second machine, register the capture shim on
both machines against one collector and one workspace mapping and let each run a short
agent session first:

```text
export MOLT_COLLECTOR_URL="<collector endpoint>"
export MOLT_COLLECTOR_TOKEN_PARAM="<parameter name holding the bearer token>"
export MOLT_INGRESS_SECRET_PARAM="<parameter name holding the shared signing secret>"
export MOLT_CLIENT_MAP="<path to the workspace-to-client mapping>"
```

The shim is `molt-hook <tool> <EVENT>`, registered as the per-tool note in
[`hooks/`](hooks) describes; the machine identifier lands in the `machine_id` column
of every Event it writes. Budget for this only if both machines are already
registered — the beat is 30 seconds and hook registration is not a demonstration.

### Beat 3 — Semantic recall changes an agent decision (32 s)

**On screen.** The same recall output, now with the `kind` and `confidence` fields in
view: a Learned_Procedure returned above the recall floor, and the agent's next action
following it.

**Said.** "Before the agent acts, it asks memory. It gets back a procedure distilled
from earlier runs, with the standing that procedure has earned — raised by runs that
succeeded, lowered by runs that failed. A procedure that has been failing sits below
the recall floor and is not returned at all, so memory withholds its own bad advice
rather than repeating it."

**Command.**

```text
molt recall "<QUERY>" --client <CLIENT-SLUG> -k 5 --json
```

The seeded corpus is built to make this beat visible: some procedures are driven
below the configured recall floor by the outcomes of the Sessions that retrieved them,
and those are the ones absent from the page. Say "absent" out loud — a viewer cannot
see a row that is not there, and the claim is only honest if it is stated as an
exclusion rather than implied by a gap.

Where the recorder has an agent tool registered, this is the beat to split the screen
on: the pre-action hook injects the recalled block in that tool's own
context-injection format, so the decision the agent takes next is visibly the one the
procedure recommends.

### Beat 4 — Residue detection (30 s)

**On screen.** The residue report, distances and bands per candidate, then the console
view of the same finding at `<CONSOLE-ORIGIN>/residue`.

**Said.** "Some of one client's code is sitting inside another client's sessions,
carrying no identifier that says so. Relational search cannot find it. This is a
vector search over stored embeddings, and every candidate carries the cosine distance
it was found at and the band that distance falls in — included outright, referred for
adjudication, or left alone."

**Command.**

```text
molt residue --client <CLIENT-SLUG>
```

**URL.** `<CONSOLE-ORIGIN>/residue`

This verb mutates nothing, which is why it is safe to run live on camera. The
thresholds it bands against are the configured pair, and the recorder can show what a
different pair would have decided with `molt sensitivity --client <CLIENT-SLUG>` if a
beat is spare — it is not, at this budget.

### Beat 5 — An Erasure_Run under a lease (40 s)

**On screen.** A dry run first, its disposition summary, then the real run's phase
lines, then the run detail at `<CONSOLE-ORIGIN>/erase/<RUN-ID>`.

**Said.** "Dry run first: it mutates nothing and prints exactly what the real run
would do. Then the real one. It takes an exclusive lease before it touches anything,
backs the cluster up before the first mutation, deletes what belongs to this client
alone, and surgically rewrites the artifacts that mix two clients so the other
client's contribution survives. Every decision is recorded per artifact."

**Commands.**

```text
molt erase --client <CLIENT-SLUG> --requester <REQUESTER-ID> \
  --justification "<JUSTIFICATION>" --dry-run
molt erase --client <CLIENT-SLUG> --requester <REQUESTER-ID> \
  --justification "<JUSTIFICATION>" --yes
```

**URL.** `<CONSOLE-ORIGIN>/erase/<RUN-ID>`

The `--yes` is not decoration: an invocation that neither confirms nor asks for a dry
run is refused as a usage fault, because the run is irreversible. Note the `run_id`
and the `certificate_object_key` the second command prints; beat six needs both.

If there is a spare second, show `<CONSOLE-ORIGIN>/erase/<RUN-ID>/redactions/` with an
artifact identifier appended: the before-and-after comparison of one rewritten body is
the strongest single frame in the recording, because it shows the surviving client's
content untouched beside the removed client's.

### Beat 6 — Certificate verification (28 s)

**On screen.** The verification output, each check named, the outcome line, and the
process exit status.

**Said.** "The certificate is signed with a key that lives outside the database, so a
principal who can rewrite every row still cannot forge it. Verification is not a
lookup: it recomputes the counts from append-only rows, re-runs the certificate's own
queries against the erased cluster, recomputes each touched session's hash chain, and
checks the signature against a retrieved public key. Zero means verified. Three would
mean the verification ran and the answer was no."

**Commands.**

```text
molt attest verify --s3-key <CERTIFICATE-KEY>
echo $?
```

A recorder holding the certificate as a file rather than an object key uses
`molt attest verify --certificate <path>` instead; the checks are the same.

Show the exit status. A verification that ran to completion and concluded `failed`
ends with its own status rather than a generic error, which is the whole reason this
verb is scriptable, and a viewer who sees the status believes the outcome line.

### Beat 7 — Close (6 s)

**On screen.** The certificate display at `<CONSOLE-ORIGIN>/certificates/<RUN-ID>`.

**Said.** "Captured, recalled, erased, and provable. One cluster, one system of
record."

**URL.** `<CONSOLE-ORIGIN>/certificates/<RUN-ID>`

## What this recording deliberately does not claim

Say none of these on camera, and do not stage a frame that implies one.

- **Tamper proofing.** The checkpoint and the certificate are tamper *evidence*. A
  rewrite becomes detectable; nothing prevents it. Overstating this is the expensive
  error, and [`platform.md`](platform.md) records why.
- **A closed replay window.** The ingest signature bounds the replayable window to the
  configured maximum request age. It does not close it.
- **A cleared corpus.** The erasure removes the named Client. The other seeded tenants
  are still there afterwards, which is the point — show `molt residue --client
  <OTHER-SLUG>` if a viewer asks.

## Related documents

- [setup.md](setup.md) — every command above in its operational context, including the
  stack deployment order beats five and six depend on.
- [platform.md](platform.md) — the measured recall latency behind beat three and the
  probed capabilities behind beats four and six.
- [protection.md](protection.md) — why the certificate in beat six cannot be produced
  by a principal who can rewrite the rows it describes.
- [cost.md](cost.md) — what running the configuration this recording is made against
  costs per month.
- [hooks/](hooks) — the per-tool registration beat two mentions.
- [glossary.md](glossary.md) — `Erasure_Run`, `Erasure_Certificate`,
  `Semantic_Residue`, `Learned_Procedure`, `Procedure_Confidence`.

_Requirements: 35.9, 13.2, 13.3, 13.9, 17.1, 17.2, 17.7, 17.9, 18.10, 18.11, 22.1,
22.9, 22.10, 25.5, 25.12, 28.1, 28.2, 28.5, 28.6, 28.8, 28.9, 49.9._
