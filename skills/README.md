# Agent skills

Three operational procedures, shipped as loadable skills rather than described
in prose: verifying an erasure certificate, sweeping for a client's semantic
residue, and auditing retention per client. Each is executable, each reads and
writes nothing, and each is loadable by a client agent that never imports this
project's Python.

## The format used

These are written in the open [Agent Skills](https://agentskills.io)
specification: one directory per skill, holding a required `SKILL.md` whose YAML
frontmatter carries the skill's declaration and whose Markdown body carries its
instructions, plus the conventional optional `scripts/` directory holding
executable code the agent may run.

```
skills/
  verify-certificate/
    SKILL.md
    scripts/verify_certificate.sh
  residue-sweep/
    SKILL.md
    scripts/residue_sweep.sh
  retention-audit/
    SKILL.md
    scripts/retention_audit.sh
```

Nothing here extends the format with a parallel manifest. The frontmatter fields
are the specification's own: `name`, `description`, `license`, `compatibility`,
`allowed-tools`, and `metadata`, the last being the specification's map of
string keys to string values for properties a client stores beyond the defined
set. Every field a reader might expect to find in a bespoke manifest is declared
there instead, under keys prefixed so they cannot collide with another
publisher's:

| Frontmatter key | Declares |
|---|---|
| `metadata.molt-inputs` | The skill's inputs, named; each is detailed in the body's input table |
| `metadata.molt-outputs` | The skill's outputs, named; each is detailed in the body's output table |
| `metadata.molt-behavior` | What the skill does, what it reads, and what it leaves unchanged |
| `metadata.molt-operations` | Every operation the skill invokes, each drawn from the read-only set below |
| `metadata.molt-entry-point` | The executable path, relative to the skill directory |
| `metadata.molt-effect` | `read_only`, for all three |
| `metadata.molt-database-role` | `reader`, the role holding `SELECT` and nothing else |
| `allowed-tools` | The pre-approved tools: the skill's own entry point, file reads, and the read-only server tools it calls |

## How an MCP-compatible client loads these

A client loads these without modification, in three steps:

1. **Discovery.** The client scans a skills directory and reads each
   `SKILL.md`'s frontmatter. The `name` and `description` fields are the whole
   startup cost; a skill's body is not read until the skill is chosen. Point the
   client at this directory, or copy a skill directory into the location the
   client scans. A skill directory is self-contained: it references no file
   outside itself, so copying one copies all of it.
2. **Activation.** When a task matches a description, the client loads that
   `SKILL.md`'s body and follows its steps. The body names the inputs to collect
   and the outputs to report, so no client-specific glue is needed.
3. **Execution.** The client runs the declared entry point with the collected
   arguments, and calls the named server tools through its own MCP session. Each
   entry point prints JSON objects on standard output and narration on standard
   error, so a client reads a stream of objects rather than parsing prose.

Two prerequisites are declared in each skill's `compatibility` field rather than
assumed: this project's command-line interface must be on `PATH`, and, for the
skills that call server tools, `MOLT_MCP_PROTOCOL_VERSION` must name the
transport revision the client negotiates. That revision is a caller's input
because a revision pinned inside a shipped definition ages against the transport
rather than with it.

## The read-only set

Every operation any skill declares comes from this set, and every entry is a
read. Two mechanisms make that structural rather than conventional: the reader
role holds `SELECT` and no `INSERT`, `UPDATE`, or `DELETE`, and the server's tool
registry contains no mutation tool, so a mutation could not be issued even by a
tool that tried.

| Operation | Surface | Reads |
|---|---|---|
| `cli:attest verify` | Certificate verification path | Certificate payload, verification query results, live counts, session chains, ledger checkpoint |
| `cli:retention` | Retention report path | Per client the jurisdiction, the interval, and the expiring and expired counts |
| `cli:recall` | Semantic recall path | Ranked artifacts with distances and outcomes |
| `cli:verify-chain` | Chain verification path | Per session the verified row count and terminal digest |
| `cli:sensitivity` | Threshold grid path | Candidate counts per threshold pair |
| `cli:mcp` | Spawns the read-only server over a transport | Nothing itself; carries the tool calls below |
| `mcp:recall_memory` | Server tool | Ranked artifacts over fleet memory |
| `mcp:lineage_ancestors` | Server tool | The artifacts an artifact was derived from |
| `mcp:lineage_descendants` | Server tool | The artifacts reachable from an artifact |
| `mcp:residue_candidates` | Server tool | Residue candidates with distances, bands, and decisions |

Operations outside this set are absent by design, not by omission. Erasure,
seeding, migration, the policy watcher, and the console are each a separate path
that mutates, and no definition here names one.

## Scope

Shipping these skills does not discharge the schema and query review obligation
these operational procedures were reviewed under; that review stands alongside
them and is recorded in `docs/reviews.md`.
