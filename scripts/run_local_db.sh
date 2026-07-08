#!/usr/bin/env bash
#
# Start, inspect, and stop a single-node insecure CockroachDB instance for the
# test suites, and print the connection string for it on standard output.
#
# The instance is a local test target only. It holds no real data, listens on
# the loopback interface, and carries no password, so nothing this script
# prints is a secret and nothing it prints is ever written to a tracked file.
#
# Every action is idempotent: starting an already-running instance prints the
# same connection string and starts no second process, and stopping an instance
# that is not running succeeds and changes nothing.
#
# Usage:
#   run_local_db.sh start    start if needed, then print the connection string
#   run_local_db.sh dsn      print the connection string without starting
#   run_local_db.sh status   report running or not running, exit 0 or 1
#   run_local_db.sh stop     stop if running, leaving the data directory
#   run_local_db.sh wipe     stop if running, then remove the state directory
#
# Diagnostics go to standard error, so standard output carries the connection
# string and nothing else and is safe to capture into a variable.

set -o errexit
set -o nounset
set -o pipefail

readonly REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly STATE_DIR="${MOLT_LOCAL_DB_DIR:-${REPO_ROOT}/.molt/localdb}"
readonly DATA_DIR="${STATE_DIR}/data"
readonly LOG_DIR="${STATE_DIR}/logs"
readonly PID_FILE="${STATE_DIR}/instance.pid"
readonly SQL_PORT="${MOLT_LOCAL_DB_PORT:-26257}"
readonly HTTP_PORT="${MOLT_LOCAL_DB_HTTP_PORT:-26258}"
# Named with a prefix so that the script sets no variable the surrounding shell
# already gives a meaning to.
readonly DB_HOST="${MOLT_LOCAL_DB_HOST:-localhost}"
readonly DB_NAME="${MOLT_LOCAL_DB_NAME:-molt_test}"
readonly DB_USER="${MOLT_LOCAL_DB_USER:-root}"
readonly BINARY="${MOLT_COCKROACH_BIN:-cockroach}"
readonly PYTHON="python3.12"
readonly READY_ATTEMPTS=60

log() {
  printf '%s\n' "$*" >&2
}

die() {
  log "error: $*"
  exit 1
}

require_binary() {
  command -v "${BINARY}" >/dev/null 2>&1 ||
    die "the CockroachDB binary '${BINARY}' is not on PATH; set MOLT_COCKROACH_BIN"
  command -v "${PYTHON}" >/dev/null 2>&1 ||
    die "${PYTHON} is not on PATH and is required to compose the connection string"
}

# The connection string is assembled by a Python step so that the user, host,
# port, and database name are percent-encoded by a library rather than by shell
# string concatenation. The value is produced at run time and never stored.
compose_dsn() {
  "${PYTHON}" - "${DB_USER}" "${DB_HOST}" "${SQL_PORT}" "${DB_NAME}" <<'PY'
import sys
from urllib.parse import quote, urlencode

user, host, port, database = sys.argv[1:5]
netloc = f"{quote(user, safe='')}@{host}:{int(port)}"
query = urlencode({"sslmode": "disable"})
sys.stdout.write(f"postgresql://{netloc}/{quote(database, safe='')}?{query}\n")
PY
}

recorded_pid() {
  [[ -f "${PID_FILE}" ]] || return 1
  local pid
  pid="$(cat "${PID_FILE}")"
  [[ -n "${pid}" ]] || return 1
  printf '%s' "${pid}"
}

# Running means a recorded process that is still alive and a port that answers.
# Both conditions matter: a stale identifier file must not make a start a no-op,
# and a live process that has not opened its port yet is not yet usable.
process_alive() {
  local pid
  pid="$(recorded_pid)" || return 1
  kill -0 "${pid}" 2>/dev/null
}

port_answers() {
  "${BINARY}" sql \
    --insecure \
    --host "${DB_HOST}:${SQL_PORT}" \
    --execute 'SELECT 1' \
    >/dev/null 2>&1
}

is_running() {
  process_alive && port_answers
}

wait_until_ready() {
  local attempt=0
  while ((attempt < READY_ATTEMPTS)); do
    if port_answers; then
      return 0
    fi
    process_alive || return 1
    attempt=$((attempt + 1))
    sleep 1
  done
  return 1
}

ensure_database() {
  "${BINARY}" sql \
    --insecure \
    --host "${DB_HOST}:${SQL_PORT}" \
    --execute "CREATE DATABASE IF NOT EXISTS ${DB_NAME}" \
    >/dev/null
}

start_instance() {
  require_binary

  if is_running; then
    log "instance already listening on ${DB_HOST}:${SQL_PORT}, starting nothing"
    ensure_database
    compose_dsn
    return 0
  fi

  if process_alive; then
    log "a recorded process is alive but not answering; waiting for it"
  else
    rm -f "${PID_FILE}"
    mkdir -p "${DATA_DIR}" "${LOG_DIR}"
    log "starting a single-node instance on ${DB_HOST}:${SQL_PORT}"
    "${BINARY}" start-single-node \
      --insecure \
      --store="${DATA_DIR}" \
      --listen-addr="${DB_HOST}:${SQL_PORT}" \
      --http-addr="${DB_HOST}:${HTTP_PORT}" \
      --log-dir="${LOG_DIR}" \
      >"${LOG_DIR}/stdout.log" 2>"${LOG_DIR}/stderr.log" &
    printf '%s\n' "$!" >"${PID_FILE}"
  fi

  if ! wait_until_ready; then
    log "the instance did not become ready; the last lines of its error log follow"
    tail -n 20 "${LOG_DIR}/stderr.log" >&2 2>/dev/null || true
    rm -f "${PID_FILE}"
    die "instance start failed"
  fi

  ensure_database
  log "instance ready"
  compose_dsn
}

stop_instance() {
  if ! process_alive; then
    rm -f "${PID_FILE}"
    log "no recorded instance is running, stopping nothing"
    return 0
  fi

  local pid
  pid="$(recorded_pid)"
  log "stopping the instance"
  kill -TERM "${pid}" 2>/dev/null || true

  local attempt=0
  while ((attempt < READY_ATTEMPTS)) && kill -0 "${pid}" 2>/dev/null; do
    attempt=$((attempt + 1))
    sleep 1
  done

  if kill -0 "${pid}" 2>/dev/null; then
    log "the instance did not exit on request, ending it forcibly"
    kill -KILL "${pid}" 2>/dev/null || true
  fi

  rm -f "${PID_FILE}"
  log "instance stopped"
}

report_status() {
  if is_running; then
    log "running and answering on ${DB_HOST}:${SQL_PORT}"
    return 0
  fi
  log "not running"
  return 1
}

main() {
  local action="${1:-start}"
  case "${action}" in
    start) start_instance ;;
    dsn)
      require_binary
      compose_dsn
      ;;
    status) report_status ;;
    stop) stop_instance ;;
    wipe)
      stop_instance
      rm -rf "${STATE_DIR}"
      log "state directory removed"
      ;;
    *)
      die "unknown action '${action}'; expected start, dsn, status, stop, or wipe"
      ;;
  esac
}

main "$@"
