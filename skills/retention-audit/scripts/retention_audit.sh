#!/usr/bin/env bash
#
# Entry point of the retention-audit skill.
#
# One read-only stage: the retention report path, called once per named client
# or once for every covered client when none is named. Standard output carries
# one JSON object per invocation. Narration goes to standard error.
#
# The stage runs under the read-only role. The role selector is set here rather
# than inherited, so the posture is a property of this file and not of the
# surrounding environment. The report path holds SELECT alone: it reads the
# configured jurisdiction, the configured interval, and the two counts, and it
# changes none of them, because the cluster enforces expiry itself.
#
# Usage:
#   retention_audit.sh [--client SLUG]...
#
# Exit status is 0 when every requested report was produced, 2 on a usage or
# configuration error, and 1 on an operational failure such as an unreachable
# cluster.

set -o errexit
set -o nounset
set -o pipefail

readonly READ_ONLY_ROLE="reader"
readonly USAGE_STATUS=2

# A slug reaches an argument vector rather than a shell string, and its shape is
# checked before it is used rather than trusted.
readonly SLUG_SHAPE='^[0-9a-z][0-9a-z-]{0,63}$'

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

# One report, for the named client or for every covered client when none is
# named. The two invocations are written out rather than assembled from an array,
# because an empty array expanded under a set variable check is a portability
# trap and the argument vector here is short enough to read twice.
report_for() {
  local report_status=0
  if (($# > 0)); then
    MOLT_DB_ROLE="${READ_ONLY_ROLE}" molt retention --json --client "$1" ||
      report_status=$?
  else
    MOLT_DB_ROLE="${READ_ONLY_ROLE}" molt retention --json ||
      report_status=$?
  fi
  ((report_status == 0)) ||
    die "the retention report reported status ${report_status}"
}

main() {
  local -a slugs=()

  while (($# > 0)); do
    case "$1" in
      --client)
        (($# >= 2)) || refuse "--client needs a slug"
        [[ "$2" =~ ${SLUG_SHAPE} ]] || refuse "--client '$2' is no client slug"
        slugs+=("$2")
        shift 2
        ;;
      *)
        refuse "unknown argument '$1'"
        ;;
    esac
  done

  command -v molt >/dev/null 2>&1 ||
    die "the command-line interface is not on PATH"

  if ((${#slugs[@]} == 0)); then
    log "auditing retention for every covered client under the ${READ_ONLY_ROLE} role"
    report_for
    return 0
  fi

  local slug
  for slug in "${slugs[@]}"; do
    log "auditing retention for ${slug} under the ${READ_ONLY_ROLE} role"
    report_for "${slug}"
  done
}

main "$@"
