#!/usr/bin/env bash
#
# Create the four least-privilege database roles, one control-plane service
# account per role, and the per-auditor read-only accounts and views.
#
# The four roles are the writer the Collector connects as, the eraser the console
# and the command-line erasure path connect as, the reader the verifier, the
# analyser, and the tool server connect as, and the watcher the policy watcher
# connects as. The privileges themselves are granted by the migrations, which are
# the record of who may do what; this script creates the roles those grants land
# on and the accounts that assume them.
#
# Credential handling has one rule: a generated credential goes from the generator
# straight into the parameter store and nowhere else. It is never printed, never
# passed as a command-line argument, never written to a tracked file, and never
# left in a temporary file after the write. Every parameter is written in the
# standard tier, which carries no per-parameter monthly charge.
#
# Idempotence comes from asking before creating. A role that exists is left alone,
# a service account that exists is left alone, and a parameter that already holds a
# value is left alone rather than rotated, so a second run changes no state.
#
# Usage:
#   provision_roles.sh --cluster NAME [--prefix /molt] [--auditor SLUG:CLIENT_SLUG]...
#
# The connection string this script administers the cluster through is read from
# the environment and is never printed. Every command-line tool is invoked with an
# argument vector.

set -o errexit
set -o nounset
set -o pipefail

readonly REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly CCLOUD="${MOLT_CCLOUD_BIN:-ccloud}"
readonly AWS_BIN="${MOLT_AWS_BIN:-aws}"
readonly SQL_CLIENT="${MOLT_COCKROACH_BIN:-cockroach}"
readonly PYTHON="python3.12"

# The four service roles, and the parameter segment each one's connection string
# is stored under.
readonly SERVICE_ROLES=(molt_writer molt_eraser molt_reader molt_watcher)

# The longest interval an auditor account may be valid for. An auditor account is
# evidence access rather than standing access, so it expires on its own.
readonly AUDITOR_MAX_VALID_DAYS=30

cluster_name=""
parameter_prefix="/molt"
auditor_specifications=()

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
      --prefix)
        (($# >= 2)) || die "--prefix takes a parameter path prefix"
        parameter_prefix="$2"
        shift 2
        ;;
      --auditor)
        (($# >= 2)) || die "--auditor takes SLUG:CLIENT_SLUG"
        auditor_specifications+=("$2")
        shift 2
        ;;
      *)
        die "unknown argument '$1'"
        ;;
    esac
  done
  [[ -n "${cluster_name}" ]] || die "--cluster is required"
  [[ "${parameter_prefix}" == /* ]] || die "the parameter prefix begins with a separator"
  [[ -n "${MOLT_ADMIN_DSN:-}" ]] ||
    die "MOLT_ADMIN_DSN must hold the administrative connection string"
}

# Every identifier that reaches a statement is held to this shape first. Names
# cannot be bound as parameters in a data-definition statement, so the defence is
# to admit only names that carry nothing a parser could read as syntax.
readonly IDENTIFIER_SHAPE='^[a-z][a-z0-9_]{0,30}$'

check_identifier() {
  [[ "$1" =~ ${IDENTIFIER_SHAPE} ]] ||
    die "'$1' is no permitted identifier; use lower-case letters, digits, and underscores"
}

# Statements are handed to the client in a file rather than as an argument, so a
# statement never appears in the process table beside the connection string. The
# connection string reaches the client through its own environment variable and is
# never printed.
run_sql() {
  local statements="$1"
  COCKROACH_URL="${MOLT_ADMIN_DSN}" "${SQL_CLIENT}" sql --file "${statements}" >/dev/null
}

sql_file() {
  local path
  path="$(mktemp)"
  chmod 600 "${path}"
  printf '%s' "$path"
}

create_service_roles() {
  local statements
  statements="$(sql_file)"
  local role
  for role in "${SERVICE_ROLES[@]}"; do
    check_identifier "${role}"
    printf 'CREATE ROLE IF NOT EXISTS %s;\n' "${role}" >>"${statements}"
  done
  log "creating the four service roles where they do not already exist"
  run_sql "${statements}"
  rm -f "${statements}"
}

# The infrastructure template declares each parameter name with a non-secret
# placeholder, because a template cannot declare an encrypted parameter and a
# template must carry no value. A parameter still holding that placeholder is
# therefore an unset parameter, and the real value replaces it. The comparison
# happens in a variable that is never printed.
readonly PLACEHOLDER_VALUE="${MOLT_PARAMETER_PLACEHOLDER:-replace-out-of-band}"

parameter_exists() {
  "${AWS_BIN}" ssm get-parameter --name "$1" --query Name --output text >/dev/null 2>&1
}

parameter_holds_placeholder() {
  local held
  held="$("${AWS_BIN}" ssm get-parameter --name "$1" --query Parameter.Value --output text \
    2>/dev/null)" || return 1
  [[ "${held}" == "${PLACEHOLDER_VALUE}" ]]
}

# A parameter counts as set only where it exists and holds something other than
# the declared placeholder, which is what makes a second run change no state while
# a first run over a freshly deployed stack still writes every credential.
parameter_is_set() {
  parameter_exists "$1" && ! parameter_holds_placeholder "$1"
}

# Replacing a placeholder means deleting it first, because the encrypted type a
# credential is written as cannot be applied to an existing plain parameter.
clear_placeholder() {
  if parameter_holds_placeholder "$1"; then
    "${AWS_BIN}" ssm delete-parameter --name "$1" >/dev/null
    log "cleared the declared placeholder at $1"
  fi
}

# Writes one value into the parameter store in the standard tier. The value
# arrives on standard input, is serialised into a private request file by the
# composer, and the request file is removed whether or not the write succeeded.
put_parameter() {
  local name="$1"
  local request
  request="$(mktemp)"
  chmod 600 "${request}"
  "${PYTHON}" "${REPO_ROOT}/scripts/compose_parameter_request.py" "${name}" >"${request}"
  local status=0
  "${AWS_BIN}" ssm put-parameter --cli-input-json "file://${request}" >/dev/null || status=$?
  rm -f "${request}"
  return "${status}"
}

generate_secret() {
  "${PYTHON}" -c 'import secrets, sys; sys.stdout.write(secrets.token_urlsafe(32))'
}

service_account_exists() {
  local listing
  listing="$("${CCLOUD}" service-account list --output json 2>/dev/null)" || return 1
  printf '%s' "${listing}" |
    "${PYTHON}" "${REPO_ROOT}/scripts/find_cluster.py" "$1" >/dev/null
}

create_service_account() {
  local role="$1"
  if service_account_exists "${role}"; then
    log "a service account for ${role} already exists, creating nothing"
    return 0
  fi
  "${CCLOUD}" service-account create --name "${role}" --description "Molt ${role} role" >/dev/null
  log "service account created for ${role}"
}

# Creates the login for one role and stores its connection string. The password is
# generated, handed to the statement file and to the parameter write, and then
# discarded with the files that held it. Nothing prints it.
provision_role_credential() {
  local role="$1"
  local parameter="${parameter_prefix}/store/dsn/${role#molt_}"
  if parameter_is_set "${parameter}"; then
    log "${parameter} already holds a value, rotating nothing"
    return 0
  fi
  clear_placeholder "${parameter}"
  local host
  host="$("${CCLOUD}" cluster info "${cluster_name}" --output json |
    "${PYTHON}" "${REPO_ROOT}/scripts/compose_role_dsn.py" --field host)"
  local secret
  secret="$(generate_secret)"
  local statements
  statements="$(sql_file)"
  {
    printf "CREATE USER IF NOT EXISTS %s_svc WITH PASSWORD '%s';\n" "${role}" "${secret}"
    printf 'GRANT %s TO %s_svc;\n' "${role}" "${role}_svc"
  } >>"${statements}"
  run_sql "${statements}"
  rm -f "${statements}"
  "${PYTHON}" "${REPO_ROOT}/scripts/compose_role_dsn.py" \
    --user "${role}_svc" --host "${host}" <<<"${secret}" |
    put_parameter "${parameter}"
  log "stored the ${role} connection string under ${parameter}"
}

# One auditor gets a read-only login that expires on its own, a schema of its own,
# and views filtered to its own tenant. Select is the only privilege granted, and
# it is granted on the views rather than on the tables, so the filter cannot be
# stepped around.
#
# HANDOVER NOTE, from the session that wrote docs/auditor.md. Nothing below was
# changed; documenting this function surfaced three places where it and the
# requirements do not line up, and the decisions belong to whoever owns this file.
# docs/auditor.md currently documents the narrower behaviour this script actually
# has, so the guide is accurate either way and needs revisiting only if the script
# changes.
#
#   1. No named as-of-attribution view. Requirement 43.11 has the Auditor_Gateway
#      expose the as-of query of Requirement 43.4 "through the read-only view set".
#      Three views are created here and none is that one. The obligation is met only
#      because the client_binding view happens to project valid_from, valid_to, and
#      superseded_by, which leaves the as-of read as a predicate the auditor writes.
#      If 43.11 means a named view, this function is one CREATE VIEW short.
#
#   2. No control-plane service account per auditor. Requirement 24.4 asks for one
#      ccloud service account per Auditor. create_service_account is called for the
#      four service roles only; an auditor gets a database login with VALID UNTIL.
#      The expiry obligation is satisfied; the control-plane account is not created.
#
#   3. molt_reader does not back these views. Migration 007 and the glossary both
#      say the reader role is what the auditor views connect with. Select is granted
#      directly to the per-auditor login here and molt_reader is never granted to
#      it. Same posture, narrower reach: this login cannot read the tables
#      molt_reader can, which is arguably the better outcome, but the three
#      statements of it disagree and one of them should move.
provision_auditor() {
  local specification="$1"
  local auditor="${specification%%:*}"
  local client="${specification#*:}"
  [[ "${auditor}" != "${specification}" ]] || die "an auditor is given as SLUG:CLIENT_SLUG"
  check_identifier "${auditor}"
  check_identifier "${client}"
  local parameter="${parameter_prefix}/auditor/${auditor}/dsn"
  if parameter_is_set "${parameter}"; then
    log "${parameter} already holds a value, rotating nothing"
    return 0
  fi
  clear_placeholder "${parameter}"
  local secret
  secret="$(generate_secret)"
  local statements
  statements="$(sql_file)"
  {
    printf "CREATE USER IF NOT EXISTS auditor_%s WITH PASSWORD '%s' VALID UNTIL '%s';\n" \
      "${auditor}" "${secret}" "$(expiry_instant)"
    printf 'CREATE SCHEMA IF NOT EXISTS auditor_%s;\n' "${auditor}"
    printf 'GRANT USAGE ON SCHEMA auditor_%s TO auditor_%s;\n' "${auditor}" "${auditor}"
    printf 'CREATE OR REPLACE VIEW auditor_%s.ledger AS SELECT * FROM ledger WHERE client_id IN (SELECT id FROM client WHERE slug = %s);\n' \
      "${auditor}" "'${client}'"
    printf 'CREATE OR REPLACE VIEW auditor_%s.erasure_run AS SELECT * FROM erasure_run WHERE client_id IN (SELECT id FROM client WHERE slug = %s);\n' \
      "${auditor}" "'${client}'"
    printf 'CREATE OR REPLACE VIEW auditor_%s.client_binding AS SELECT * FROM client_binding WHERE client_id IN (SELECT id FROM client WHERE slug = %s);\n' \
      "${auditor}" "'${client}'"
    printf 'GRANT SELECT ON auditor_%s.ledger TO auditor_%s;\n' "${auditor}" "${auditor}"
    printf 'GRANT SELECT ON auditor_%s.erasure_run TO auditor_%s;\n' "${auditor}" "${auditor}"
    printf 'GRANT SELECT ON auditor_%s.client_binding TO auditor_%s;\n' "${auditor}" "${auditor}"
  } >>"${statements}"
  run_sql "${statements}"
  rm -f "${statements}"
  local host
  host="$("${CCLOUD}" cluster info "${cluster_name}" --output json |
    "${PYTHON}" "${REPO_ROOT}/scripts/compose_role_dsn.py" --field host)"
  "${PYTHON}" "${REPO_ROOT}/scripts/compose_role_dsn.py" \
    --user "auditor_${auditor}" --host "${host}" <<<"${secret}" |
    put_parameter "${parameter}"
  log "auditor ${auditor} provisioned read-only over its own tenant"
}

# The expiry instant is computed at run time from the configured maximum interval,
# so no instant is ever written into a tracked file.
expiry_instant() {
  "${PYTHON}" "${REPO_ROOT}/scripts/compose_role_dsn.py" \
    --expiry-days "${AUDITOR_MAX_VALID_DAYS}"
}

main() {
  parse_arguments "$@"
  require_binary "${CCLOUD}"
  require_binary "${AWS_BIN}"
  require_binary "${SQL_CLIENT}"
  require_binary "${PYTHON}"

  create_service_roles
  local role
  for role in "${SERVICE_ROLES[@]}"; do
    create_service_account "${role}"
    provision_role_credential "${role}"
  done

  local specification
  for specification in ${auditor_specifications[@]+"${auditor_specifications[@]}"}; do
    provision_auditor "${specification}"
  done

  log "role provisioning complete; no credential value was printed"
}

main "$@"
