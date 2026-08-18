#!/usr/bin/env bash
#
# Delete every stack in the reverse of the deployment order, releasing the object
# retention on each certificate version first.
#
# The retention release is why this script exists rather than a line of
# documentation. The certificate bucket is created with object lock enabled and a
# default retention in governance mode, which is what makes a certificate immutable
# while it matters. A locked version cannot be deleted and a bucket holding versions
# cannot be deleted either, so a plain stack deletion stops halfway and waits for
# somebody to clear the bucket by hand. Governance mode is precisely the mode a
# holder of the bypass permission may lift, so this script lifts it version by
# version, empties the bucket, and then deletes the stacks.
#
# Deleting a stack that is already gone succeeds and changes nothing, so the script
# is safe to re-run after a partial failure.
#
# Usage:
#   teardown.sh [--prefix STACK_PREFIX] [--region REGION] [--keep-certificates]

set -o errexit
set -o nounset
set -o pipefail

readonly INFRA_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd -- "${INFRA_DIR}/.." && pwd)"
readonly AWS_BIN="${MOLT_AWS_BIN:-aws}"

# The interpreter the repository's own helpers run under, resolved in the deployment
# script's order so a teardown and the deployment it reverses agree on which
# interpreter they mean rather than differing by which one was invoked.
if [[ -n "${MOLT_PYTHON:-}" ]]; then
  readonly PYTHON="${MOLT_PYTHON}"
elif [[ -x "${HOME}/.molt-venv/bin/python" ]]; then
  readonly PYTHON="${HOME}/.molt-venv/bin/python"
elif [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  readonly PYTHON="${REPO_ROOT}/.venv/bin/python"
else
  readonly PYTHON="python3.12"
fi

# The reverse of the deployment order.
readonly TEARDOWN_ORDER=(
  observability
  mcp
  watcher
  cdn
  gateway
  console
  collector
  storage
  kms
  parameters
  network
)

stack_prefix="molt"
region_argument=()
keep_certificates="false"

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
      --prefix)
        (($# >= 2)) || die "--prefix takes a stack name prefix"
        stack_prefix="$2"
        shift 2
        ;;
      --region)
        (($# >= 2)) || die "--region takes a region name"
        region_argument=(--region "$2")
        shift 2
        ;;
      --keep-certificates)
        keep_certificates="true"
        shift
        ;;
      *)
        die "unknown argument '$1'"
        ;;
    esac
  done
}

stack_name() {
  printf '%s-%s' "${stack_prefix}" "$1"
}

stack_exists() {
  "${AWS_BIN}" cloudformation describe-stacks \
    ${region_argument[@]+"${region_argument[@]}"} \
    --stack-name "$(stack_name "$1")" \
    --query "Stacks[0].StackName" \
    --output text >/dev/null 2>&1
}

stack_output() {
  "${AWS_BIN}" cloudformation describe-stacks \
    ${region_argument[@]+"${region_argument[@]}"} \
    --stack-name "$(stack_name "$1")" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" \
    --output text
}

# The retention document that ends a retention now. The instant is produced at run
# time from the clock rather than written down, so no instant is ever held in a
# tracked file, and the document is serialised by a library rather than assembled
# as a shell string.
expired_retention() {
  "${PYTHON}" -c 'import json, sys
from datetime import UTC, datetime
json.dump(
    {
        "Mode": "GOVERNANCE",
        "RetainUntilDate": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    },
    sys.stdout,
)'
}

# Lifts the governance-mode retention on one object version and deletes it. The
# bypass is stated on the retention call, which is the operation the mode admits;
# a compliance-mode retention would admit no such call, which is exactly why the
# bucket is created in governance mode with a short interval.
release_and_delete_version() {
  local bucket="$1"
  local key="$2"
  local version="$3"
  "${AWS_BIN}" s3api put-object-retention \
    ${region_argument[@]+"${region_argument[@]}"} \
    --bucket "${bucket}" \
    --key "${key}" \
    --version-id "${version}" \
    --bypass-governance-retention \
    --retention "$(expired_retention)" \
    >/dev/null 2>&1 || true
  "${AWS_BIN}" s3api delete-object \
    ${region_argument[@]+"${region_argument[@]}"} \
    --bucket "${bucket}" \
    --key "${key}" \
    --version-id "${version}" \
    --bypass-governance-retention \
    >/dev/null
}

empty_certificate_bucket() {
  if [[ "${keep_certificates}" == "true" ]]; then
    log "leaving the certificate bucket in place on request"
    return 0
  fi
  if ! stack_exists storage; then
    log "no storage stack is deployed, releasing nothing"
    return 0
  fi
  local bucket
  bucket="$(stack_output storage CertificateBucketName)"
  [[ -n "${bucket}" && "${bucket}" != "None" ]] || return 0
  log "releasing the retention on every certificate version in ${bucket}"
  local key version
  while IFS=$'\t' read -r key version; do
    [[ -n "${key}" ]] || continue
    release_and_delete_version "${bucket}" "${key}" "${version}"
  done < <("${AWS_BIN}" s3api list-object-versions \
    ${region_argument[@]+"${region_argument[@]}"} \
    --bucket "${bucket}" \
    --query "concat(Versions[].[Key,VersionId] || \`[]\`, DeleteMarkers[].[Key,VersionId] || \`[]\`)" \
    --output text)
  log "certificate versions released and removed"
}

delete_stack() {
  local stack="$1"
  if ! stack_exists "${stack}"; then
    log "${stack} is not deployed, deleting nothing"
    return 0
  fi
  log "deleting ${stack}"
  "${AWS_BIN}" cloudformation delete-stack \
    ${region_argument[@]+"${region_argument[@]}"} \
    --stack-name "$(stack_name "${stack}")"
  "${AWS_BIN}" cloudformation wait stack-delete-complete \
    ${region_argument[@]+"${region_argument[@]}"} \
    --stack-name "$(stack_name "${stack}")"
  log "${stack} deleted"
}

main() {
  parse_arguments "$@"
  command -v "${AWS_BIN}" >/dev/null 2>&1 || die "${AWS_BIN} is not on PATH"
  command -v "${PYTHON}" >/dev/null 2>&1 || die "${PYTHON} is not on PATH"
  empty_certificate_bucket
  local stack
  for stack in "${TEARDOWN_ORDER[@]}"; do
    delete_stack "${stack}"
  done
  log "teardown complete with no manual step outstanding"
}

main "$@"
