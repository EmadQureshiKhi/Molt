#!/usr/bin/env bash
#
# Entry point of the residue-sweep skill.
#
# One read-only stage: the residue candidate tool, called for the named client
# over the MCP stdio transport. Standard output carries the tool's own response
# objects, one JSON object per line. Narration goes to standard error.
#
# The stage runs under the read-only role. The role selector is set here rather
# than inherited, so the posture is a property of this file and not of the
# surrounding environment. The server this script spawns holds SELECT alone and
# its tool registry contains no mutation tool, so the sweep is a search and a
# threshold comparison and nothing else.
#
# Usage:
#   residue_sweep.sh --client SLUG [--auto-include-threshold FLOAT]
#                                  [--review-threshold FLOAT] [--limit INT]
#
# Exit status is 0 when the sweep ran, 2 on a usage or configuration error, and
# 1 on an operational failure such as an unreachable cluster.

set -o errexit
set -o nounset
set -o pipefail

readonly SKILL_NAME="residue-sweep"
readonly READ_ONLY_ROLE="reader"
readonly USAGE_STATUS=2

# Argument values are interpolated into a request body, so their shapes are
# checked before they are used rather than trusted.
readonly SLUG_SHAPE='^[0-9a-z][0-9a-z-]{0,63}$'
readonly DISTANCE_SHAPE='^(0|1)(\.[0-9]{1,6})?$'
readonly COUNT_SHAPE='^[1-9][0-9]{0,5}$'

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

# The frames of one stdio session: the handshake, then one residue candidate
# call carrying the client slug and whichever thresholds the caller named.
sweep_frames() {
  local revision="$1"
  local arguments="$2"
  printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"%s",' \
    "${revision}"
  printf '"capabilities":{},"clientInfo":{"name":"%s","version":"1"}}}\n' "${SKILL_NAME}"
  printf '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n'
  printf '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":'
  printf '{"name":"residue_candidates","arguments":{%s}}}\n' "${arguments}"
}

main() {
  local slug=""
  local auto_include=""
  local review=""
  local limit=""

  while (($# > 0)); do
    case "$1" in
      --client)
        (($# >= 2)) || refuse "--client needs a slug"
        [[ "$2" =~ ${SLUG_SHAPE} ]] || refuse "--client '$2' is no client slug"
        slug="$2"
        shift 2
        ;;
      --auto-include-threshold)
        (($# >= 2)) || refuse "--auto-include-threshold needs a cosine distance"
        [[ "$2" =~ ${DISTANCE_SHAPE} ]] ||
          refuse "--auto-include-threshold '$2' is no cosine distance"
        auto_include="$2"
        shift 2
        ;;
      --review-threshold)
        (($# >= 2)) || refuse "--review-threshold needs a cosine distance"
        [[ "$2" =~ ${DISTANCE_SHAPE} ]] ||
          refuse "--review-threshold '$2' is no cosine distance"
        review="$2"
        shift 2
        ;;
      --limit)
        (($# >= 2)) || refuse "--limit needs a positive count"
        [[ "$2" =~ ${COUNT_SHAPE} ]] || refuse "--limit '$2' is no positive count"
        limit="$2"
        shift 2
        ;;
      *)
        refuse "unknown argument '$1'"
        ;;
    esac
  done

  [[ -n "${slug}" ]] || refuse "name the client with --client SLUG"

  command -v molt >/dev/null 2>&1 ||
    die "the command-line interface is not on PATH"

  local arguments
  arguments="$(printf '"client_slug":"%s"' "${slug}")"
  if [[ -n "${auto_include}" ]]; then
    arguments+="$(printf ',"auto_include_threshold":%s' "${auto_include}")"
  fi
  if [[ -n "${review}" ]]; then
    arguments+="$(printf ',"review_threshold":%s' "${review}")"
  fi
  if [[ -n "${limit}" ]]; then
    arguments+="$(printf ',"limit":%s' "${limit}")"
  fi

  local revision
  revision="$(protocol_revision)"

  log "sweeping for residue of ${slug} under the ${READ_ONLY_ROLE} role, mutating nothing"
  local sweep_status=0
  sweep_frames "${revision}" "${arguments}" |
    MOLT_DB_ROLE="${READ_ONLY_ROLE}" molt mcp --transport stdio ||
    sweep_status=$?
  ((sweep_status == 0)) ||
    die "the residue stage reported status ${sweep_status}"
}

main "$@"
