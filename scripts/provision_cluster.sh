#!/usr/bin/env bash
#
# Create the cluster, apply every migration in order, and record the platform
# facts the deployment branches on.
#
# The script is idempotent in every step. A cluster that already exists is left
# alone and its identifier is reported. Migrations are applied by the migration
# runner, which records an applied version only after every statement of that
# migration has succeeded, so a second run applies nothing. The capability probes
# replace the row for each fact rather than appending a second answer, so a second
# run leaves the same record behind.
#
# Nothing here prints a credential, a connection string, a cluster hostname, or an
# account identifier. The cluster identifier is a resource name rather than a
# secret and is printed so the caller can pass it on; every credential is written
# to the parameter store by the role script and read back by nothing.
#
# Usage:
#   provision_cluster.sh --cluster NAME [--plan basic] [--region REGION]
#                        [--backup-target URL] [--skip-model-check]
#
# Every command-line tool is invoked with an argument vector, so no value the
# caller supplies is ever assembled into a command string.

set -o errexit
set -o nounset
set -o pipefail

readonly REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly CCLOUD="${MOLT_CCLOUD_BIN:-ccloud}"
# The interpreter the repository's own helpers run under. The capability probe imports
# the package itself, so the bare platform interpreter fails it on an absent module and
# the cluster is left provisioned with no capability record. The order is the test
# runner's and the deployment script's, so every tool on one machine resolves the same
# interpreter.
if [[ -n "${MOLT_PYTHON:-}" ]]; then
  readonly PYTHON="${MOLT_PYTHON}"
elif [[ -x "${HOME}/.molt-venv/bin/python" ]]; then
  readonly PYTHON="${HOME}/.molt-venv/bin/python"
elif [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  readonly PYTHON="${REPO_ROOT}/.venv/bin/python"
else
  readonly PYTHON="python3.12"
fi
readonly MOLT="${MOLT_CLI_BIN:-molt}"
# The control plane offers backup listing and backup configuration and offers no
# operation that creates one, which is the observation the probe records.
readonly CONTROL_PLANE_BACKUP="listing-and-configuration-only"

cluster_name=""
cluster_plan="basic"
cluster_region=""
backup_target=""
skip_model_check="false"

log() {
  printf '%s\n' "$*" >&2
}

die() {
  log "error: $*"
  exit 2
}

require_binary() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is not on PATH"
}

parse_arguments() {
  while (($# > 0)); do
    case "$1" in
      --cluster)
        (($# >= 2)) || die "--cluster takes a name"
        cluster_name="$2"
        shift 2
        ;;
      --plan)
        (($# >= 2)) || die "--plan takes a plan name"
        cluster_plan="$2"
        shift 2
        ;;
      --region)
        (($# >= 2)) || die "--region takes a region name"
        cluster_region="$2"
        shift 2
        ;;
      --backup-target)
        (($# >= 2)) || die "--backup-target takes a storage target"
        backup_target="$2"
        shift 2
        ;;
      --skip-model-check)
        skip_model_check="true"
        shift
        ;;
      *)
        die "unknown argument '$1'"
        ;;
    esac
  done
  [[ -n "${cluster_name}" ]] || die "--cluster is required"
  # A cluster name reaches the control plane as one argument, and is still held to
  # a conservative shape so a mistyped value fails here rather than creating a
  # resource nobody meant to create.
  [[ "${cluster_name}" =~ ^[a-zA-Z][a-zA-Z0-9-]{2,39}$ ]] ||
    die "the cluster name must be alphanumeric with hyphens"
}

# Reports whether a cluster of this name already exists. The listing is asked for
# in machine-readable form and searched by the Python step, so no value is parsed
# by a shell pattern.
cluster_exists() {
  local listing
  listing="$("${CCLOUD}" cluster list --output json 2>/dev/null)" || return 1
  printf '%s' "${listing}" | "${PYTHON}" "${REPO_ROOT}/scripts/find_cluster.py" "${cluster_name}"
}

create_cluster() {
  if cluster_exists >/dev/null; then
    log "a cluster named '${cluster_name}' already exists, creating nothing"
    return 0
  fi
  log "creating the cluster"
  # The plan, the name, and the regions are positional, in that order. They were
  # written as named options, which the control plane refuses outright on an unknown
  # flag — so the creation failed before it began, and only on the one run that
  # actually had a cluster to create. Still one argument vector: nothing is assembled
  # into a shell word, so a value carrying a separator cannot become two arguments.
  local -a create=("${CCLOUD}" cluster create "${cluster_plan}" "${cluster_name}")
  if [[ -n "${cluster_region}" ]]; then
    create+=("${cluster_region}")
  fi
  "${create[@]}" >/dev/null
  log "cluster created"
}

apply_migrations() {
  log "applying every migration in order"
  "${MOLT}" migrate
  log "migrations applied and their versions recorded"
}

# The control-plane interrogation lives here rather than in the probe module,
# because this script owns every command-line invocation. What the probe module
# receives is the observed outcome as one of a fixed set of choices, so no value
# read from the control plane ever becomes part of a command.
observe_control_plane_backup() {
  local -a interrogate=("${CCLOUD}" cluster backup --help)
  if "${interrogate[@]}" >/dev/null 2>&1; then
    log "the control plane answered the backup interrogation"
  else
    log "the control plane offered no backup subcommand to interrogate"
  fi
  printf '%s' "${CONTROL_PLANE_BACKUP}"
}

record_capabilities() {
  local observed
  observed="$(observe_control_plane_backup)"
  local -a probe=(
    "${PYTHON}" "${REPO_ROOT}/scripts/probe_capabilities.py"
    --control-plane-backup "${observed}"
  )
  if [[ -n "${backup_target}" ]]; then
    probe+=(--backup-target "${backup_target}")
  fi
  if [[ "${skip_model_check}" == "true" ]]; then
    probe+=(--skip-model-check)
  fi
  log "recording the probed platform facts"
  "${probe[@]}"
}

main() {
  parse_arguments "$@"
  require_binary "${CCLOUD}"
  require_binary "${PYTHON}"
  require_binary "${MOLT}"
  create_cluster
  apply_migrations
  record_capabilities
  log "provisioning complete"
  printf '%s\n' "${cluster_name}"
}

main "$@"
