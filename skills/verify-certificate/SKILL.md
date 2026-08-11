---
name: verify-certificate
description: >-
  Verifies a Molt erasure certificate against a live CockroachDB cluster and
  reports the machine-readable overall outcome together with every failed
  check, then confirms through the lineage tools that no descendant of an
  erased artifact survives. Use when a departing client's reviewer asks whether
  an erasure actually happened, when a certificate needs checking independently
  of the party that produced it, or when a lineage remnant of erased material
  is suspected.
license: MIT
compatibility: >-
  Requires the molt command-line interface on PATH, network reach to a
  CockroachDB cluster through the read-only reader role, and a Molt MCP server
  the interface can spawn over the stdio transport. Requires
  MOLT_MCP_PROTOCOL_VERSION to name the transport revision the caller
  negotiates, because a revision pinned inside a shipped definition ages
  against the transport rather than with it.
allowed-tools: >-
  Read Bash(./scripts/verify_certificate.sh:*) mcp__molt__lineage_ancestors
  mcp__molt__lineage_descendants
metadata:
  molt-entry-point: scripts/verify_certificate.sh
  molt-inputs: certificate, s3-key, bucket, checkpoint, artifact-id
  molt-outputs: >-
    outcome, failed_checks, verification_query_row_counts, count_agreement,
    chain_mismatch, checkpoint_outcome, lineage_ancestors, lineage_descendants
  molt-behavior: >-
    Runs the certificate verification path against the live cluster, which
    recomputes the canonical payload digest, checks the signature against the
    public key held outside the cluster, executes every verification query the
    certificate carries, re-executes the before-state and after-state counts,
    verifies the hash chain of every session the certificate names, and
    verifies the ledger checkpoint the certificate names. Then calls the
    lineage ancestor and descendant tools for each artifact identifier the
    caller names, so a surviving derived descendant of erased material is
    reported rather than assumed absent. Emits one JSON object per stage on
    standard output and returns the verification path's own exit status, so a
    failed outcome is a non-zero status rather than a line of prose. Reads
    only: no stage writes a row, and the database role carries SELECT alone.
  molt-operations: >-
    cli:attest verify, cli:mcp, mcp:lineage_ancestors, mcp:lineage_descendants
  molt-effect: read_only
  molt-database-role: reader
---

# Verify an erasure certificate against a live cluster

An erasure certificate is a signed claim that material belonging to one client
was removed. This skill checks that claim against the cluster the claim is
about, and reports what failed rather than only whether something failed.

## Behavior

The entry point performs two read-only stages and prints one JSON object per
stage on standard output, in stage order:

1. **Certificate verification.** The verification path recomputes the canonical
   payload digest, checks the attached signature against the public key
   retrieved from the key service, executes every verification query the
   certificate carries against the live cluster and reports each query's row
   count, re-executes the before-state and after-state counts and reports
   agreement or disagreement with the counts the certificate records, verifies
   the hash chain of every session the certificate names and reports the first
   mismatching sequence number where one exists, and verifies the ledger
   checkpoint the certificate names. The stage reports an overall outcome of
   `verified` or `failed` together with the list of failed checks.
2. **Lineage confirmation.** For each artifact identifier the caller names, the
   lineage ancestor tool and the lineage descendant tool are called through the
   MCP stdio transport. An erased artifact has no surviving ancestor and no
   surviving descendant, so a non-empty result from either call is a finding
   that the certificate's claim is incomplete.

The exit status is the verification path's own: `0` when the outcome is
`verified`, `3` when the outcome is `failed`, `2` on a usage or configuration
error, and `1` on an operational failure such as an unreachable cluster.

## Inputs

| Input | Required | Meaning |
|---|---|---|
| `certificate` | one of `certificate` or `s3-key` | Filesystem path to the signed certificate document |
| `s3-key` | one of `certificate` or `s3-key` | Object key of the signed certificate in the certificate bucket |
| `bucket` | with `s3-key` | Name of the bucket holding the certificate object |
| `checkpoint` | no | Identifier of a ledger checkpoint to verify beside the certificate |
| `artifact-id` | yes, repeatable | Artifact identifier named by the certificate, checked for surviving lineage |

`MOLT_MCP_PROTOCOL_VERSION` must name the transport revision to negotiate. The
entry point refuses to run without it and names it in the refusal, rather than
guessing a revision on the caller's behalf.

## Outputs

Standard output carries one JSON object per line. Standard error carries
narration only.

| Output | Meaning |
|---|---|
| `outcome` | `verified` or `failed`, the machine-readable overall result |
| `failed_checks` | The list of checks that failed, empty when the outcome is `verified` |
| `verification_query_row_counts` | Per verification query, the row count the live cluster returned; any non-zero count means the erasure was incomplete and names the surviving artifact identifiers |
| `count_agreement` | Agreement or disagreement between the re-executed before-state and after-state counts and the counts the certificate records |
| `chain_mismatch` | The first mismatching sequence number per session, absent when every chain verifies |
| `checkpoint_outcome` | The outcome of verifying the ledger checkpoint the certificate names, including whether a disagreement is accounted for by recorded dispositions |
| `lineage_ancestors` | Per named artifact, the ancestors the lineage tool returned; an empty result is the expected outcome for erased material |
| `lineage_descendants` | Per named artifact, the descendants the lineage tool returned; an empty result is the expected outcome for erased material |

## Read-only posture

Every operation this skill declares is a read. The verification path connects
with the reader role, which holds `SELECT` and no `INSERT`, `UPDATE`, or
`DELETE`; the MCP server connects with the same role and exposes no mutation
tool, so a mutation could not be issued even by a tool that tried. The entry
point sets the role selector explicitly rather than inheriting whichever role
the surrounding environment happens to name.

| Declared operation | Kind |
|---|---|
| `cli:attest verify` | Certificate verification path, reader role |
| `cli:mcp` | Spawns the read-only MCP server over the stdio transport |
| `mcp:lineage_ancestors` | Lineage ancestor retrieval, no mutation |
| `mcp:lineage_descendants` | Lineage descendant retrieval, no mutation |

## Steps

1. Ask the caller for the certificate location and for the artifact
   identifiers to check, or read them from the certificate the caller supplies.
2. Run `scripts/verify_certificate.sh` with those arguments.
3. Read the first output object. If `outcome` is `failed`, report every entry
   of `failed_checks` with the detail its own field carries, rather than
   reporting only that verification failed.
4. Read the lineage objects. Report any artifact whose ancestor or descendant
   result is non-empty as a surviving remnant, naming the artifact identifiers
   returned.
5. Report the exit status alongside the outcome, because a reviewer scripting
   this skill acts on the status rather than on the narration.
