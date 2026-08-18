# Model providers

The delivered account holds zero on-demand inference quota and cannot have it
raised, so the cloud provider's own inference service answers every call with a
refusal. Nothing in Molt changed to absorb that. Two protocols and a name-to-module
registry mean that which model Molt calls is one configuration value, so **provider
availability is a configuration concern rather than an architectural one**
(Requirement 37.14). The abstraction earned its cost once, on the day the quota
turned out to be zero, and it will earn it again the day the quota is restored:
that restoration is also one configuration value and no source change.

The abstraction costs something, and the cost is easy to state. Every implementation
has to present the same face, a builder taking resolved configuration and answering a
protocol, so an implementation needing a client, a region, and a credential and one
needing a host and a credential both hide their constructor shape behind `build`. Both
protocol surfaces then have to be complete enough that no caller reaches around them,
which is why the prompt shape, the result shape with its four token counts, and the
probe shape all live in `src/molt/providers/__init__.py` rather than in each
implementation.

## The two roles and the three consumers

The count is asymmetric because the two text-role components name different models
against one provider. A record that collapsed them would answer *which provider*
without answering *which model*.

| Consumer | Role | Provider selection key | Model identifier key | Delivered selection |
|---|---|---|---|---|
| Embedder | `EmbeddingProvider` | `MOLT_EMBEDDING_PROVIDER` | `MOLT_EMBEDDING_MODEL_ID` | `external` — a code-specialised retrieval model reached over the Voyage AI embeddings interface |
| Adjudicator | `TextProvider` | `MOLT_TEXT_PROVIDER` | `MOLT_ADJUDICATION_MODEL_ID` | `external` — a prompt-caching text model reached over the Anthropic messages interface |
| Redaction_Rewriter | `TextProvider` | `MOLT_TEXT_PROVIDER` | `MOLT_REWRITE_MODEL_ID` | `external` — the same provider as the Adjudicator, its own model key |

Both selection keys default to `bedrock` in `src/molt/config/resolve.py`, and
`config.example.toml` names `bedrock` for both. **Bedrock is the documented default
implementation of both roles** (Requirements 37.3, 37.6): `src/molt/providers/bedrock.py`
is registered under both role names, and one builder answers for both because a
builder is handed a configuration rather than a role and so cannot be told which
registration it is answering. Where both roles select it, the object reports the
embedding model's identifier, since that is the identifier written onto every
vector row and therefore the one an incorrect answer would make durable.

The delivered demonstration configuration selects `external` for both roles
(Requirement 37.7). The embedding choice is an objective argument rather than a
preference: residue detection searches for semantically similar *source code*, so a
code-specialised retrieval model is the right target (Requirement 37.10). The text
choice is a cost argument: adjudication makes one call per review-band candidate
against a byte-identical task instruction and query excerpt, and prompt caching
turns that shared prefix into one paid write and many cheap reads.

## Region and model identifiers: what is and is not grounded

The repository grounds no deployment region and no concrete model identifier, and
this document will not invent either.

- `MOLT_BEDROCK_REGION` carries no default; `config.example.toml` names the
  placeholder `REPLACE_WITH_DEPLOYMENT_REGION`. Requirement 34.10 constrains the
  region rather than naming it — it must be a region in which every model the
  configured providers require is available — and Requirement 34.11 puts the
  verification in the Provisioner, which reports any unreachable identifier and
  exits non-zero before deployment completes. So the region is an operator input
  checked at provision time, and **the chosen region is not recorded anywhere in
  this repository**.
- All three model identifier keys carry no default, and the example configuration
  names `REPLACE_WITH_EMBEDDING_MODEL_ID`, `REPLACE_WITH_ADJUDICATION_MODEL_ID`,
  and `REPLACE_WITH_REWRITE_MODEL_ID`. **No verified model identifier is grounded
  in the repository.** What is grounded is the shape of each: the embedding model
  is verified to answer exactly 1024 dimensions (Requirement 37.8) and the text
  model is verified to complete an adjudication-shaped call and report
  prompt-cache token fields.

What the code does pin, because the interface requires it and no operator chooses
it, is the service each external implementation talks to: the Voyage AI embeddings
path and the Anthropic messages path, both as module constants in
`external_embedding.py` and `external_text.py`.

## From a configuration value to a running call

```mermaid
flowchart TD
    conf["Configuration surface<br/>MOLT_EMBEDDING_PROVIDER, MOLT_TEXT_PROVIDER"]
    names["validate_selected_names<br/>checks both names against the registry keys"]
    unknown["UnknownProviderError<br/>names the value and lists the keys<br/>nothing imported, nothing called"]
    entry["ProviderEntry<br/>module path and builder attribute"]
    imp["importlib.import_module<br/>first import of any client library"]
    build["build(configuration)<br/>credential loaded, transport constructed"]
    gate["validate_at_startup<br/>probe, then compare width to 1024"]
    refuse["ProviderWidthMismatchError<br/>both widths printed, exit status 2<br/>before any vector exists"]
    report["StartupReport<br/>three RoleSelections, prompt-cache finding"]
    call["A running call<br/>embed() or generate(prompt)"]
    conf --> names
    names -->|name absent from registry| unknown
    names -->|name accepted| entry --> imp --> build --> gate
    gate -->|width other than 1024| refuse
    gate -->|width matches| report --> call
```

Three properties of that path are deliberate.

**Resolution is lazy and by module path.** A registry entry holds a module path and
a builder name, not a builder, so importing `registry.py` drags in no client
library. That is what lets the credential-free suites collect and the strict type
check run with no provider package installed, and it means selecting one provider
never pays the import cost of the others.

**An unknown name fails before anything is imported.** The refusal names the value
and lists the keys on offer, so the operator's next action is to pick one. A
registered name whose module is missing or exposes no callable `build` is a
different class of fault — the name was accepted, so what failed is the
implementation behind it.

**The width check is a startup gate, not a per-row check.** `SCHEMA_VECTOR_DIMENSIONS`
is 1024 because the stored column and the distributed vector index are declared at
that width. A mismatch left to the column constraint would be discovered one insert
at a time, after a run had already begun writing. The gate probes once, compares,
prints both widths, and leaves the process with status 2 — a configuration fault,
distinct from an operational one, because nothing was attempted and nothing will
succeed until a value changes (Requirement 37.9). A probe that answers no width is
held to the width the provider declares, since the declared width is what every
later call would produce.

## Normalisation, per implementation

Every cosine threshold in this design is expressed in cosine space while the
distributed vector index orders by L2 distance, and those two orderings coincide
only over unit vectors. So write-time normalisation is load-bearing rather than
defensive, and which implementation is selected decides whether it does any work
(Requirement 37.16):

| Implementation | Returns unit-normalised vectors | Consequence |
|---|---|---|
| `external` (delivered) | Yes, as recorded against the model's own behaviour | The Embedder's scaling is a no-op in effect, not in obligation |
| `bedrock` (documented default) | No | The Embedder's scaling is the only thing making the index ordering agree with the thresholds |

Neither implementation *relies* on that: `external_embedding.py` returns vectors
exactly as the model answered them and says so, and normalisation happens on the
write path, which is the one place that has to agree with the index's distance
function. See [memory-tiers.md](memory-tiers.md) for
where the vectors live and [platform.md](platform.md) for the index the ordering
comes from.

## Failure, retries, and credentials

Every cause collapses to one unavailability fault. Unreachable, throttled, timed
out, refused, and malformed all raise `ModelUnavailableError`, because every
caller's answer to all five is identical — retry a bounded number of times, then
fail closed — so distinguishing them would invite a branch nothing asks for. The
cause is named in the message and in a log record, which is where a cause belongs.

Retries are bounded and back off, and the delay doubles from a small base up to a cap.
A refusal and a malformed answer are not retried, since neither becomes true by being
asked again.

| Setting | Default | Note |
|---|---|---|
| `MOLT_PROVIDER_MAX_RETRIES` | 3 | Counts attempts *after* the first |
| `MOLT_PROVIDER_TIMEOUT_SECONDS` | 30 | Per call |
| `MOLT_EMBEDDING_BATCH_SIZE` | 25 | Texts per embedding call |

Credentials resolve from a parameter name or an operator-provided file and from
nowhere else (Requirement 37.11). `selector.load_credential` maps a role to its
pair of keys and delegates the resolution rule to the secret accessors rather than
restating it, because two implementations of one resolution rule is exactly the
drift the placeholder discipline cannot survive. The loaded value goes straight
into the transport: the provider builds a request body and reads a response body,
never sees a header, and never holds the credential. No failure record carries a
header, a request body, or a response body as a field, so no credential value can
reach a stream by accident (Requirement 37.12). The threat and its residue are in
[threat-model.md](threat-model.md).

## Prompt caching is read from the model, not assumed

`MOLT_PROMPT_CACHE_ENABLED` defaults to `auto`. The operator's preference narrows
the model's own report and never widens it: claiming a capability the model does
not report would mark a boundary the provider ignores, and the point of reading the
capability is to mark it only where marking it means something. An unreachable text
provider is recorded as reporting no capability rather than raised as a startup
failure, because every component calling a text model already fails closed on an
unavailable one. The finding travels back on the `StartupReport` for the caller to
persist as the `text_provider_prompt_cache` row; the selector holds no connection
and writes nothing.

## Related documents

- [platform.md](platform.md) — the probed platform facts, including the vector
  index whose operator class makes normalisation load-bearing.
- [threat-model.md](threat-model.md) — credential compromise, provider credential
  leakage, and prompt injection into an adjudication prompt.
- [glossary.md](glossary.md) — `Embedding_Provider`, `Text_Provider`,
  `Provider_Selector`, `Bedrock`, `External_Embedding_Service`.
- [setup.md](setup.md) — the operator steps that supply the region, the model
  identifiers, and the credentials.

_Requirements: 10.1, 10.2, 10.13, 34.3, 34.10, 34.11, 37.1–37.16, 38.3._
