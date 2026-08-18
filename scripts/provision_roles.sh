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

# The interpreter the repository's own helpers run under. It has to be one that holds
# the pinned dependencies and the installed package, which the bare platform
# interpreter does not, so naming that unconditionally made every helper fail on an
# absent module rather than on anything to do with the provisioning. The order is the
# test runner's and the deployment script's — an override, then an environment in the
# working tree, then the platform interpreter — so every tool on one machine resolves
# the same interpreter.
if [[ -n "${MOLT_PYTHON:-}" ]]; then
  readonly PYTHON="${MOLT_PYTHON}"
elif [[ -x "${HOME}/.molt-venv/bin/python" ]]; then
  readonly PYTHON="${HOME}/.molt-venv/bin/python"
elif [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  readonly PYTHON="${REPO_ROOT}/.venv/bin/python"
else
  readonly PYTHON="python3.12"
fi

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
  # The name is positional. It was written as a named option and the control plane
  # refused the whole invocation on an unknown flag, which is a failure that arrives
  # only when a deployment reaches this step for the first time.
  "${CCLOUD}" service-account create "${role}" --description "Molt ${role} role" >/dev/null
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
  # Creation and the password are separate statements, and the grant names the login
  # rather than the login plus another suffix.
  #
  # The suffix was applied twice — the format string carried it and so did the
  # argument — so the grant named a login nothing had created and the statement file
  # failed partway through, after the login existed.
  #
  # That partial state is why the password is set by its own statement. Creating with
  # a password under "if not exists" is a no-op when the login is already there, so a
  # second run after a partial failure left the login holding the first run's password
  # while storing the second run's in the parameter: a credential that cannot
  # authenticate, stored as though it could, with nothing failing until a deployed
  # process tried it. Reaching this point at all means the parameter held no value, so
  # any existing login's password is unknown and setting it is the only way to make the
  # stored string true.
  {
    printf 'CREATE USER IF NOT EXISTS %s_svc;\n' "${role}"
    printf "ALTER USER %s_svc WITH PASSWORD '%s';\n" "${role}" "${secret}"
    printf 'GRANT %s TO %s_svc;\n' "${role}" "${role}"
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
# A handover note listed three places where this function and the requirements did
# not line up. Two of them are now closed here, and the third is left as it stands
# because closing it would widen an auditor's reach rather than narrow it.
#
#   1. Closed. The as-of-attribution view of Requirement 43.11 is created below as
#      auditor_<SLUG>.attribution_as_of, beside the other three and filtered the
#      same way. It belongs here rather than in a migration for the same reason the
#      other three do: the filter names one tenant's slug, so the object exists once
#      per auditor and cannot be declared by a schema file that knows no auditor.
#      A view takes no parameter, so the as-of instant cannot be bound inside it.
#      What the view does is name the read and fix its projection to the columns the
#      binding_as_of index stores, leaving the interval containment predicate as a
#      WHERE the auditor writes over valid_from and valid_to. That is 43.11 in
#      substance: the read is a named member of the view set rather than an
#      incidental consequence of another view projecting the right columns, but the
#      caller-supplied instant of Requirement 43.4 stays caller-supplied.
#
#   2. Closed. One control-plane service account per auditor, per Requirement 24.4,
#      through the same create_service_account path the four service roles use. It
#      runs before the credential check, so a re-run over an auditor whose
#      connection string is already stored still establishes the account. The
#      expiry obligation is unchanged: the database login keeps its VALID UNTIL.
#
#   3. Left as it stands. Migration 007 and the glossary both say molt_reader is
#      what the auditor views connect with, and SELECT is granted directly to the
#      per-auditor login here instead. Same posture, narrower reach: this login
#      cannot read the tables molt_reader can. Granting molt_reader here would
#      widen an untrusted third party's access to close a documentation
#      disagreement, so the disagreement is reported rather than resolved by this
#      file, and migration 007's prose or the glossary is where it should move.
provision_auditor() {
  local specification="$1"
  local auditor="${specification%%:*}"
  local client="${specification#*:}"
  [[ "${auditor}" != "${specification}" ]] || die "an auditor is given as SLUG:CLIENT_SLUG"
  check_identifier "${auditor}"
  check_identifier "${client}"
  create_service_account "auditor_${auditor}"
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
    # Creation and the credential are separate statements, for the reason the service
    # role's are: creating with a password under "if not exists" is a no-op when the
    # login already exists, so a re-run after a partial failure would store a password
    # the login does not hold. The validity bound is set alongside it, because a
    # re-issued credential with the first run's expiry is a login that stops working
    # earlier than the parameter says.
    printf 'CREATE USER IF NOT EXISTS auditor_%s;\n' "${auditor}"
    printf "ALTER USER auditor_%s WITH PASSWORD '%s' VALID UNTIL '%s';\n" \
      "${auditor}" "${secret}" "$(expiry_instant)"
    printf 'CREATE SCHEMA IF NOT EXISTS auditor_%s;\n' "${auditor}"
    printf 'GRANT USAGE ON SCHEMA auditor_%s TO auditor_%s;\n' "${auditor}" "${auditor}"
    printf 'CREATE OR REPLACE VIEW auditor_%s.ledger AS SELECT * FROM ledger WHERE client_id IN (SELECT id FROM client WHERE slug = %s);\n' \
      "${auditor}" "'${client}'"
    printf 'CREATE OR REPLACE VIEW auditor_%s.erasure_run AS SELECT * FROM erasure_run WHERE client_id IN (SELECT id FROM client WHERE slug = %s);\n' \
      "${auditor}" "'${client}'"
    printf 'CREATE OR REPLACE VIEW auditor_%s.client_binding AS SELECT * FROM client_binding WHERE client_id IN (SELECT id FROM client WHERE slug = %s);\n' \
      "${auditor}" "'${client}'"
    # The as-of-attribution read, named. The projection is exactly the key and
    # stored columns of the binding_as_of index, so the read stays a range scan
    # over one artifact's versions with no row fetch, which is what holds the bound
    # the as-of query is required to answer within. The artifact kind is
    # deliberately absent for that reason; the client_binding view beside this one
    # carries it. The instant is the auditor's: the interval is half-open, so the
    # containment predicate is valid_from <= the instant AND (valid_to IS NULL OR
    # valid_to > the instant).
    printf 'CREATE OR REPLACE VIEW auditor_%s.attribution_as_of AS SELECT id, artifact_id, client_id, method, confidence, valid_from, valid_to, superseded_by FROM client_binding WHERE client_id IN (SELECT id FROM client WHERE slug = %s);\n' \
      "${auditor}" "'${client}'"
    printf 'GRANT SELECT ON auditor_%s.ledger TO auditor_%s;\n' "${auditor}" "${auditor}"
    printf 'GRANT SELECT ON auditor_%s.erasure_run TO auditor_%s;\n' "${auditor}" "${auditor}"
    printf 'GRANT SELECT ON auditor_%s.client_binding TO auditor_%s;\n' "${auditor}" "${auditor}"
    printf 'GRANT SELECT ON auditor_%s.attribution_as_of TO auditor_%s;\n' "${auditor}" "${auditor}"
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
