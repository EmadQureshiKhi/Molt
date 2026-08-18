#!/usr/bin/env bash
#
# Deploy every stack in order, validating each stack's parameters before it is
# created and resolving each stack's cross-stack values from the outputs of the
# stacks already deployed.
#
# The ordering is not arbitrary. The network, the parameter names, the three roles,
# the signing key, and the bucket exist before anything that reads them; the two
# function stacks run under roles the roles stack created; the regional endpoints are
# created after the functions they invoke, because a permission naming a function that
# does not exist is refused; the distribution needs an origin that serves anonymous
# requests, which is the regional endpoint rather than the functions' own; the tool
# server joins the task cluster the watcher stack creates.
#
# The roles stack sits third for a reason a deployment established: the key policy
# names the console role as its one signing principal and the bucket policy names all
# three roles, and a principal that does not exist yet makes the key fail to create
# rather than making it permissive. The roles it creates carry only the permissions
# whose resource is constructible from the account, the region, and a parameter, so
# it resolves nothing from a later stack; the key stack and the storage stack attach
# the rest to those roles by name. That is why the key and the bucket take role
# *names* here and no resolved role value. Deploying a stack that is
# already current changes nothing, so the whole script is safe to re-run and is the
# means of applying a template change.
#
# Nothing here holds a secret. Every secret is a parameter name the templates
# declare and the provisioning scripts fill, so no credential appears in a
# deployment argument, in a parameter file, or in a stack event.
#
# Usage:
#   deploy.sh [--params PATH] [--prefix STACK_PREFIX] [--region REGION]
#             [--through STACK] [--skip STACK]... [--dry-run]
#
# --through stops after the named stack, having deployed every stack up to it in
# order. The deployment is genuinely staged rather than one pass: four stacks need no
# application code and can be created first, the cluster roles are provisioned only
# once the parameter names exist, the two function stacks need an archive in the
# bucket, and the two task stacks need an image in a registry. Without a way to stop,
# a first pass reaches a function stack before any archive exists and fails there,
# which is a rollback rather than a staging point. The staged sequence is set out in
# the setup document and this flag is what makes it executable.
#
# --skip omits one named stack and may be given more than once. It exists because a
# stack can be undeployable for a reason that has nothing to do with this repository:
# an account may be refused a resource type outright, as a new account is refused a
# content distribution until the provider verifies it, and that refusal arrives as a
# create failure partway along the order. Without a way past it, every stack after the
# refused one is unreachable even though none of them depends on it. Skipping is
# deliberately not silent — each omission is logged as one — because a stack that was
# never created is a part of the deployment that is not there, and a later stack
# resolving a value from it will say so rather than deploy something half-wired.

set -o errexit
set -o nounset
set -o pipefail

readonly INFRA_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd -- "${INFRA_DIR}/.." && pwd)"
readonly TEMPLATE_DIR="${INFRA_DIR}/templates"
readonly AWS_BIN="${MOLT_AWS_BIN:-aws}"

# The interpreter the parameter validator runs under. It has to be one that holds the
# pinned document parser, which the bare platform interpreter does not: the validator
# imports it to read a template, so naming `python3.12` unconditionally made every
# validation fail on an absent module. The order matches the test runner's — an
# override, then an environment in the working tree, then the platform interpreter —
# so both tools resolve the same interpreter on the same machine.
if [[ -n "${MOLT_PYTHON:-}" ]]; then
  readonly PYTHON="${MOLT_PYTHON}"
elif [[ -x "${HOME}/.molt-venv/bin/python" ]]; then
  readonly PYTHON="${HOME}/.molt-venv/bin/python"
elif [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  readonly PYTHON="${REPO_ROOT}/.venv/bin/python"
else
  readonly PYTHON="python3.12"
fi

# The deployment order. Each entry is a stack key, which is also the template file
# name and the section name in the parameter file.
readonly STACK_ORDER=(
  network
  parameters
  roles
  kms
  storage
  collector
  console
  gateway
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
# The last stack this invocation deploys, empty for every stack. Named rather than
# counted, so a stage names where it stops instead of how many it takes.
through=""
# The stacks this invocation omits. Named rather than numbered for the same reason the
# stopping point is: an omission states which part of the deployment is absent.
skipped=()

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
      --through)
        (($# >= 2)) || die "--through takes a stack name"
        through="$2"
        shift 2
        ;;
      --skip)
        (($# >= 2)) || die "--skip takes a stack name"
        skipped+=("$2")
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
  if [[ -n "${through}" ]]; then
    named_stack "${through}" ||
      die "--through names '${through}', which is no stack of the deployment order"
  fi
  local omitted
  for omitted in ${skipped[@]+"${skipped[@]}"}; do
    named_stack "${omitted}" ||
      die "--skip names '${omitted}', which is no stack of the deployment order"
  done
}

# Whether one name is a stack of the deployment order. Both flags validate through it,
# so a misspelled name fails before anything is created rather than silently doing
# nothing — a skip that matches no stack would otherwise deploy the stack it was meant
# to omit and report success.
named_stack() {
  local candidate
  for candidate in "${STACK_ORDER[@]}"; do
    [[ "${candidate}" == "$1" ]] && return 0
  done
  return 1
}

is_skipped() {
  local omitted
  for omitted in ${skipped[@]+"${skipped[@]}"}; do
    [[ "${omitted}" == "$1" ]] && return 0
  done
  return 1
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
    collector)
      printf 'CollectorExecutionRoleArn=%s\n' "$(stack_output roles CollectorExecutionRoleArn)"
      ;;
    console)
      printf 'ConsoleExecutionRoleArn=%s\n' "$(stack_output roles ConsoleExecutionRoleArn)"
      printf 'SigningKeyArn=%s\n' "$(stack_output kms SigningKeyArn)"
      printf 'CertificateBucketName=%s\n' "$(stack_output storage CertificateBucketName)"
      ;;
    cdn)
      # The distribution's origin is the regional endpoint rather than the function's own,
      # because the function's own cannot serve an anonymous request on an unverified
      # account and a distribution in front of an endpoint that refuses everyone would
      # refuse everyone too.
      printf 'ConsoleOriginDomain=%s\n' "$(stack_output gateway ConsoleEndpointDomain)"
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
    collector)
      printf 'CollectorExecutionRoleArn\n'
      ;;
    console)
      printf 'ConsoleExecutionRoleArn\nSigningKeyArn\nCertificateBucketName\n'
      ;;
    cdn)
      printf 'ConsoleOriginDomain\n'
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

  # The validation is captured and its status checked, rather than read through a
  # process substitution. A substitution reports the status of the loop that read it
  # and never the status of the command that wrote it, so a validator that failed —
  # crashing on an absent module, refusing a missing parameter — produced no lines,
  # the loop succeeded over nothing, and the stack was deployed with no overrides at
  # all while the log said its parameters were complete. A validation nobody can fail
  # is worse than no validation, because it reads as one.
  local validated
  if ! validated="$(validate_stack "${stack}")"; then
    die "the parameters of ${stack} did not validate, so nothing was deployed"
  fi
  while IFS= read -r line; do
    [[ -n "${line}" ]] || continue
    overrides+=("${line}")
  done <<< "${validated}"
  ((${#overrides[@]} > 0)) ||
    die "the validation of ${stack} named no parameter, so nothing was deployed"

  if [[ "${dry_run}" == "true" ]]; then
    log "${stack}: parameters complete, deploying nothing"
    return 0
  fi

  # The cross-stack values, checked the same way and for the same reason. Each is read
  # from an earlier stack's outputs, so an earlier stack that was never deployed, or an
  # output renamed, yields an empty value rather than an error — and an empty signing
  # key or an empty bucket name deploys a function that refuses at startup for a value
  # the deployment appeared to supply. A name with nothing after the separator is
  # therefore refused here.
  local resolved
  if ! resolved="$(resolved_overrides "${stack}")"; then
    die "the cross-stack values of ${stack} could not be read, so nothing was deployed"
  fi
  while IFS= read -r line; do
    [[ -n "${line}" ]] || continue
    [[ "${line}" == *=?* ]] ||
      die "the cross-stack value ${line%%=*} of ${stack} resolved empty; deploy the stack that outputs it first"
    overrides+=("${line}")
  done <<< "${resolved}"

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
    if is_skipped "${stack}"; then
      log "skipping ${stack} as asked; whatever it would have created is absent"
    else
      deploy_stack "${stack}"
    fi
    if [[ -n "${through}" && "${stack}" == "${through}" ]]; then
      log "stopped after ${stack} as asked; the stacks beyond it are untouched"
      return 0
    fi
  done
  log "every stack deployed in order"
}

main "$@"
