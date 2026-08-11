#!/usr/bin/env bash
#
# Entry point of the verify-certificate skill.
#
# Two read-only stages, in order: the certificate verification path against a
# live cluster, then the lineage ancestor and descendant tools for each artifact
# identifier the caller names. Standard output carries one JSON object per line,
# so a caller reads a stream of objects rather than parsing prose. Narration
# goes to standard error.
#
# Both stages run under the read-only role. The role selector is set here rather
# than inherited, so the posture is a property of this file and not of the
# surrounding environment. The verification path holds SELECT alone, and the MCP
# server this script spawns exposes no mutation tool.
#
# Usage:
#   verify_certificate.sh --certificate PATH   --artifact-id ID [--artifact-id ID]...
#   verify_certificate.sh --s3-key KEY --bucket NAME --artifact-id ID [...]
#                        [--checkpoint UUID]
#
# Exit status is the verification path's own: 0 when the outcome is verified,
# 3 when the outcome is failed, 2 on a usage or configuration error, and 1 on an
# operational failure.

set -o errexit
set -o nounset
set -o pipefail

readonly SKILL_NAME="verify-certificate"
readonly READ_ONLY_ROLE="reader"
readonly USAGE_STATUS=2

# An identifier is interpolated into a request body, so its shape is checked
# before it is used rather than trusted.
readonly IDENTIFIER_SHAPE='^[0-9A-Za-z][0-9A-Za-z-]{0,63}$'

log() {
  printf '%s\n' "$*" >&2
}

die() {
  log "error: $*"
  exit 1
}

refuse() {
  log "usage: $*"
  exit "${USAGE_STATUS}"
}

# The transport revision is supplied by the caller. A revision pinned inside a
# shipped definition ages against the transport, so the caller names one and the
# refusal names the variable rather than guessing on the caller's behalf.
protocol_revision() {
  local revision="${MOLT_MCP_PROTOCOL_VERSION:-}"
  [[ -n "${revision}" ]] ||
    refuse "MOLT_MCP_PROTOCOL_VERSION must name the transport revision to negotiate"
  printf '%s' "${revision}"
}

# The frames of one stdio session: the handshake, then one ancestor call and one
# descendant call per named artifact. Both tools are read-only.
lineage_frames() {
  local revision="$1"
  shift
  printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"%s",' \
    "${revision}"
  printf '"capabilities":{},"clientInfo":{"name":"%s","version":"1"}}}\n' "${SKILL_NAME}"
  printf '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n'
  local identifier
  local next=2
  for identifier in "$@"; do
    printf '{"jsonrpc":"2.0","id":%d,"method":"tools/call","params":' "${next}"
    printf '{"name":"lineage_ancestors","arguments":{"artifact_id":"%s"}}}\n' "${identifier}"
    next=$((next + 1))
    printf '{"jsonrpc":"2.0","id":%d,"method":"tools/call","params":' "${next}"
    printf '{"name":"lineage_descendants","arguments":{"artifact_id":"%s"}}}\n' "${identifier}"
    next=$((next + 1))
  done
}

main() {
  local -a verify_arguments=()
  local -a identifiers=()
  local certificate=""
  local object_key=""
  local bucket=""
  local checkpoint=""

  while (($# > 0)); do
    case "$1" in
      --certificate)
        (($# >= 2)) || refuse "--certificate needs a path"
        certificate="$2"
        shift 2
        ;;
      --s3-key)
        (($# >= 2)) || refuse "--s3-key needs an object key"
        object_key="$2"
        shift 2
        ;;
      --bucket)
        (($# >= 2)) || refuse "--bucket needs a name"
        bucket="$2"
        shift 2
        ;;
      --checkpoint)
        (($# >= 2)) || refuse "--checkpoint needs an identifier"
        checkpoint="$2"
        shift 2
        ;;
      --artifact-id)
        (($# >= 2)) || refuse "--artifact-id needs an identifier"
        [[ "$2" =~ ${IDENTIFIER_SHAPE} ]] ||
          refuse "--artifact-id '$2' is no artifact identifier"
        identifiers+=("$2")
        shift 2
        ;;
      *)
        refuse "unknown argument '$1'"
        ;;
    esac
  done

  if [[ -n "${certificate}" && -n "${object_key}" ]]; then
    refuse "name either --certificate or --s3-key, not both"
  fi
  if [[ -n "${certificate}" ]]; then
    verify_arguments+=(--certificate "${certificate}")
  elif [[ -n "${object_key}" ]]; then
    verify_arguments+=(--s3-key "${object_key}")
    [[ -n "${bucket}" ]] || refuse "--s3-key needs --bucket"
    verify_arguments+=(--bucket "${bucket}")
  else
    refuse "name one of --certificate or --s3-key"
  fi
  if [[ -n "${checkpoint}" ]]; then
    verify_arguments+=(--checkpoint "${checkpoint}")
  fi
  ((${#identifiers[@]} > 0)) ||
    refuse "name at least one --artifact-id to check for surviving lineage"

  command -v molt >/dev/null 2>&1 ||
    die "the command-line interface is not on PATH"

  local revision
  revision="$(protocol_revision)"

  log "verifying the certificate against the live cluster under the ${READ_ONLY_ROLE} role"
  local outcome_status=0
  MOLT_DB_ROLE="${READ_ONLY_ROLE}" molt attest verify --json "${verify_arguments[@]}" ||
    outcome_status=$?
  case "${outcome_status}" in
    0 | 3) ;;
    *) die "the verification path reported status ${outcome_status}" ;;
  esac

  log "confirming through the lineage tools that no erased artifact retains an edge"
  local lineage_status=0
  lineage_frames "${revision}" "${identifiers[@]}" |
    MOLT_DB_ROLE="${READ_ONLY_ROLE}" molt mcp --transport stdio ||
    lineage_status=$?
  ((lineage_status == 0)) ||
    die "the lineage stage reported status ${lineage_status}"

  exit "${outcome_status}"
}

main "$@"
