#!/usr/bin/env bash
#
# Pull the cluster audit logs for a caller-supplied window through the control
# plane command-line tool.
#
# The window is supplied by the caller rather than defaulted to a fixed span,
# because an audit pull answers a question somebody is asking and the span of that
# question is theirs. Both bounds are passed through to the control plane as
# arguments of an argument vector, so nothing the caller supplies is assembled into
# a command string.
#
# The pull is read-only and idempotent: it creates nothing, changes nothing, and
# two runs over the same window fetch the same records. Records are written to the
# caller's chosen destination or to standard output, and the destination file is
# created with owner-only permissions because an audit record names principals and
# statements.
#
# Nothing here prints or stores a credential. The control-plane tool reads its own
# authentication from its own configuration, which this script neither reads nor
# writes.
#
# Usage:
#   pull_audit_log.sh --cluster NAME --from INSTANT --to INSTANT [--output PATH]

set -o errexit
set -o nounset
set -o pipefail

readonly CCLOUD="${MOLT_CCLOUD_BIN:-ccloud}"

cluster_name=""
window_start=""
window_end=""
destination=""

log() {
  printf '%s\n' "$*" >&2
}

die() {
  log "error: $*"
  exit 2
}

parse_arguments() {
  while (($# > 0)); do
    case "$1" in
      --cluster)
        (($# >= 2)) || die "--cluster takes a name"
        cluster_name="$2"
        shift 2
        ;;
      --from)
        (($# >= 2)) || die "--from takes an instant"
        window_start="$2"
        shift 2
        ;;
      --to)
        (($# >= 2)) || die "--to takes an instant"
        window_end="$2"
        shift 2
        ;;
      --output)
        (($# >= 2)) || die "--output takes a path"
        destination="$2"
        shift 2
        ;;
      *)
        die "unknown argument '$1'"
        ;;
    esac
  done
  [[ -n "${cluster_name}" ]] || die "--cluster is required"
  [[ -n "${window_start}" ]] || die "--from is required"
  [[ -n "${window_end}" ]] || die "--to is required"
}

pull() {
  local -a fetch=(
    "${CCLOUD}" cluster audit-log list
    --cluster "${cluster_name}"
    --from "${window_start}"
    --to "${window_end}"
    --output json
  )
  if [[ -z "${destination}" ]]; then
    "${fetch[@]}"
    return 0
  fi
  : >"${destination}"
  chmod 600 "${destination}"
  "${fetch[@]}" >"${destination}"
  log "audit records for the requested window written to ${destination}"
}

main() {
  parse_arguments "$@"
  command -v "${CCLOUD}" >/dev/null 2>&1 || die "${CCLOUD} is not on PATH"
  pull
}

main "$@"
