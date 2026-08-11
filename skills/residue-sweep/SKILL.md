---
name: residue-sweep
description: >-
  Runs a semantic residue sweep for one named client against a live CockroachDB
  cluster and reports every candidate with its cosine distance, its threshold
  band, and its inclusion decision, mutating nothing. Use when checking whether
  material belonging to a client survives under another name, when choosing
  thresholds before an erasure, or when a reviewer asks what an erasure would
  reach beyond the rows that name the client outright.
license: MIT
compatibility: >-
  Requires the molt command-line interface on PATH, network reach to a
  CockroachDB cluster through the read-only reader role, and a Molt MCP server
  the interface can spawn over the stdio transport. Requires
  MOLT_MCP_PROTOCOL_VERSION to name the transport revision the caller
  negotiates, because a revision pinned inside a shipped definition ages
  against the transport rather than with it.
allowed-tools: >-
  Read Bash(./scripts/residue_sweep.sh:*) mcp__molt__residue_candidates
metadata:
  molt-entry-point: scripts/residue_sweep.sh
  molt-inputs: client-slug, auto-include-threshold, review-threshold, limit
  molt-outputs: >-
    artifact_id, artifact_kind, cosine_distance, band, decision, candidate_count
  molt-behavior: >-
    Calls the residue candidate tool for the named client, which builds query
    embeddings from the material explicitly bound to that client, searches the
    vector index for embeddings within the review threshold, excludes anything
    the explicit sweep already covers, and reports each candidate with its
    cosine distance, the band the distance falls in, and the resulting
    inclusion decision. The sweep is a search and a comparison and nothing
    else: it records no candidate row, it starts no erasure run, and it changes
    no memory content. Emits the tool's own response objects on standard
    output, one per line, and returns a non-zero status only when the sweep
    could not be performed.
  molt-operations: cli:mcp, mcp:residue_candidates
  molt-effect: read_only
  molt-database-role: reader
---

# Run a residue sweep for one client

Explicit links find the material that names a client. A residue sweep finds the
material that does not: a pasted fragment, a paraphrased design note, a derived
artifact whose ancestry was never recorded. This skill runs that search and
reports what it finds, without acting on it.

## Behavior

The entry point calls the residue candidate tool once for the named client over
the MCP stdio transport, and prints the tool's response objects on standard
output, one JSON object per line.

The tool builds one or more query embeddings from the text of artifacts
explicitly bound to the named client, searches the vector index for embeddings
whose cosine distance to a query embedding is at most the review threshold, and
excludes every artifact the explicit sweep already covers. Each surviving
candidate is reported with its distance, its band, and its decision:

| Band | Distance | Decision |
|---|---|---|
| Auto-include | at most the auto-include threshold | `include`, decided by distance alone |
| Review | above the auto-include threshold, at most the review threshold | referred for adjudication rather than decided here |

The defaults are a cosine distance of `0.20` for auto-inclusion and `0.45` for
review. Both are overridable per invocation, which is what makes this skill
useful for choosing thresholds rather than only for applying them.

Nothing is mutated. The sweep performs a vector search and a threshold
comparison; it records no candidate row, it opens no erasure run, and it
removes nothing. A caller who wants material removed runs an erasure, which is
a separate and deliberately non-read-only path this skill does not name.

## Inputs

| Input | Required | Meaning |
|---|---|---|
| `client-slug` | yes | Slug of the client whose residue is being searched for |
| `auto-include-threshold` | no | Cosine distance at or below which a candidate is included by distance alone |
| `review-threshold` | no | Cosine distance above which a candidate is not a candidate at all |
| `limit` | no | Maximum number of candidates to return, bounded further by the server's own result ceiling |

`MOLT_MCP_PROTOCOL_VERSION` must name the transport revision to negotiate. The
entry point refuses to run without it and names it in the refusal, rather than
guessing a revision on the caller's behalf.

## Outputs

Standard output carries the tool's response objects, one per line. Standard
error carries narration only.

| Output | Meaning |
|---|---|
| `artifact_id` | Identifier of the candidate artifact |
| `artifact_kind` | Kind of the candidate artifact |
| `cosine_distance` | Distance from the nearest query embedding, the number the bands are read against |
| `band` | Whether the distance falls in the auto-include band or the review band |
| `decision` | `include` for an auto-included candidate, or a referral for one that needs adjudication |
| `candidate_count` | Number of candidates returned, after the explicit-sweep exclusion and the result bound |

## Read-only posture

| Declared operation | Kind |
|---|---|
| `cli:mcp` | Spawns the read-only MCP server over the stdio transport |
| `mcp:residue_candidates` | Vector search and threshold comparison, no mutation |

The MCP server connects with the reader role, which holds `SELECT` and no
`INSERT`, `UPDATE`, or `DELETE`, and its tool registry contains no mutation
tool. The read-only posture is therefore a property of the role grant and the
registry rather than a promise this definition makes.

## Steps

1. Ask the caller for the client slug, and for thresholds if the caller wants
   something other than the defaults.
2. Run `scripts/residue_sweep.sh` with those arguments.
3. Report the candidates ordered by distance, ascending, so the nearest match
   is read first.
4. Separate the two bands in the report. An auto-included candidate is decided;
   a review-band candidate is a question for an adjudicator and should be
   presented as one rather than as a finding.
5. State the thresholds used beside the counts. A candidate count without its
   thresholds is not interpretable, because moving a threshold moves the count.
