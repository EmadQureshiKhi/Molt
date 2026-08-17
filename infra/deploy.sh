#!/usr/bin/env bash
#
# Deploy every stack in order, validating each stack's parameters before it is
# created and resolving each stack's cross-stack values from the outputs of the
# stacks already deployed.
#
# The ordering is not arbitrary. The network, the parameter names, the signing key,
# and the bucket exist before anything that reads them; the console stack needs the
# key and the bucket; the distribution needs the console endpoint; the tool server
# joins the task cluster the watcher stack creates. Deploying a stack that is
# already current changes nothing, so the whole script is safe to re-run and is the
# means of applying a template change.
#
# Nothing here holds a secret. Every secret is a parameter name the templates
# declare and the provisioning scripts fill, so no credential appears in a
# deployment argument, in a parameter file, or in a stack event.
#
# Usage:
#   deploy.sh [--params PATH] [--prefix STACK_PREFIX] [--region REGION] [--dry-run]

set -o errexit
set -o nounset
set -o pipefail

readonly INFRA_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd -- "${INFRA_DIR}/.." && pwd)"
readonly TEMPLATE_DIR="${INFRA_DIR}/templates"
readonly AWS_BIN="${MOLT_AWS_BIN:-aws}"
readonly PYTHON="python3.12"

# The deployment order. Each entry is a stack key, which is also the template file
# name and the section name in the parameter file.
readonly STACK_ORDER=(
  network
  parameters
  kms
  storage
  collector
  console
  cdn
  watcher
  mcp
  observability
)

# The stacks whose resources are named by a policy or a role, which is what
# requires the deployment to acknowledge role creation.
readonly CAPABILITY_FLAG="CAPABILITY_NAMED_IAM"

params_file="${INFRA_DIR}/params/demo.json"
stack_prefix="molt"
region_argument=()
dry_run="false"

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
      --params)
        (($# >= 2)) || die "--params takes a path"
        params_file="$2"
        shift 2
        ;;
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
      --dry-run)
        dry_run="true"
        shift
        ;;
      *)
        die "unknown argument '$1'"
        ;;
    esac
  done
  [[ -f "${params_file}" ]] || die "the parameter file ${params_file} does not exist"
}

stack_name() {
  printf '%s-%s' "${stack_prefix}" "$1"
}

template_for() {
  printf '%s/%s.yaml' "${TEMPLATE_DIR}" "$1"
}

stack_output() {
  local stack="$1"
  local key="$2"
  "${AWS_BIN}" cloudformation describe-stacks \
    ${region_argument[@]+"${region_argument[@]}"} \
    --stack-name "$(stack_name "${stack}")" \
    --query "Stacks[0].Outputs[?OutputKey=='${key}'].OutputValue" \
    --output text
}

# The domain of a function endpoint, taken from the endpoint by a parser rather
# than by trimming characters. The value arrives on standard input so it is never
# assembled into a command.
domain_of() {
  "${PYTHON}" -c 'import sys
from urllib.parse import urlsplit
sys.stdout.write(urlsplit(sys.stdin.read().strip()).netloc)'
}

# The values a stack takes from an earlier stack rather than from the parameter
# file. Each is printed as one assignment per line.
resolved_overrides() {
  local stack="$1"
  case "${stack}" in
    console)
      printf 'SigningKeyArn=%s\n' "$(stack_output kms SigningKeyArn)"
      printf 'CertificateBucketArn=%s\n' "$(stack_output storage CertificateBucketArn)"
      ;;
    cdn)
      printf 'ConsoleFunctionUrlDomain=%s\n' \
        "$(stack_output console ConsoleFunctionUrl | domain_of)"
      ;;
    watcher)
      printf 'PublicSubnetIds=%s\n' "$(stack_output network PublicSubnetIds)"
      printf 'TaskSecurityGroupId=%s\n' "$(stack_output network TaskSecurityGroupId)"
      ;;
    mcp)
      printf 'ClusterName=%s\n' "$(stack_output watcher ClusterName)"
      printf 'PublicSubnetIds=%s\n' "$(stack_output network PublicSubnetIds)"
      printf 'TaskSecurityGroupId=%s\n' "$(stack_output network TaskSecurityGroupId)"
      ;;
    *) ;;
  esac
}

# The names of those same values, which the validator counts as supplied rather
# than missing. Each name is printed on a line of its own.
resolved_names() {
  case "$1" in
    console)
      printf 'SigningKeyArn\nCertificateBucketArn\n'
      ;;
    cdn)
      printf 'ConsoleFunctionUrlDomain\n'
      ;;
    watcher)
      printf 'PublicSubnetIds\nTaskSecurityGroupId\n'
      ;;
    mcp)
      printf 'ClusterName\nPublicSubnetIds\nTaskSecurityGroupId\n'
      ;;
    *) ;;
  esac
}

validate_stack() {
  local stack="$1"
  local -a validate=(
    "${PYTHON}" "${REPO_ROOT}/scripts/validate_stack_params.py"
    --template "$(template_for "${stack}")"
    --params "${params_file}"
    --stack "${stack}"
    --print-overrides
  )
  local -a resolved=()
  local name
  while IFS= read -r name; do
    [[ -n "${name}" ]] || continue
    resolved+=(--resolved "${name}")
  done < <(resolved_names "${stack}")
  validate+=(${resolved[@]+"${resolved[@]}"})
  "${validate[@]}"
}

deploy_stack() {
  local stack="$1"
  log "validating the parameters of ${stack}"
  local -a overrides=()
  local line
  while IFS= read -r line; do
    [[ -n "${line}" ]] || continue
    overrides+=("${line}")
  done < <(validate_stack "${stack}")

  if [[ "${dry_run}" == "true" ]]; then
    log "${stack}: parameters complete, deploying nothing"
    return 0
  fi

  while IFS= read -r line; do
    [[ -n "${line}" ]] || continue
    overrides+=("${line}")
  done < <(resolved_overrides "${stack}")

  log "deploying ${stack}"
  "${AWS_BIN}" cloudformation deploy \
    ${region_argument[@]+"${region_argument[@]}"} \
    --stack-name "$(stack_name "${stack}")" \
    --template-file "$(template_for "${stack}")" \
    --capabilities "${CAPABILITY_FLAG}" \
    --no-fail-on-empty-changeset \
    --parameter-overrides ${overrides[@]+"${overrides[@]}"}
  log "${stack} deployed"
}

main() {
  parse_arguments "$@"
  command -v "${PYTHON}" >/dev/null 2>&1 || die "${PYTHON} is not on PATH"
  if [[ "${dry_run}" != "true" ]]; then
    command -v "${AWS_BIN}" >/dev/null 2>&1 || die "${AWS_BIN} is not on PATH"
  fi
  local stack
  for stack in "${STACK_ORDER[@]}"; do
    [[ -f "$(template_for "${stack}")" ]] || die "no template for stack ${stack}"
    deploy_stack "${stack}"
  done
  log "every stack deployed in order"
}

main "$@"
