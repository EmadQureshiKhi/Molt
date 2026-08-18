#!/usr/bin/env bash
# Run the suites the fast way, which is not the same way for all of them.
#
# The whole tree runs in roughly a third of the time it takes serially, and the split
# below is the reason. Three groups, each run the way it is actually bounded:
#
#   1. The static gates run once, serially. They shell out to the linter, the formatter,
#      the type checker, and the hygiene scanner over the whole tree; each of those is
#      already parallel inside itself, and running several at once makes them contend
#      for the same cores and the same type-check cache rather than finish sooner.
#
#   2. The credential-free suites run in parallel, excluding the gate suite. What is
#      excluded matters: `tests/quality` drives the same whole-tree tools as group one
#      through a subprocess, so running it under several workers reproduces exactly the
#      contention group one exists to avoid.
#
#   3. The instance-backed suites run in parallel, distributed by file. This is where
#      the time was: every module builds a schema of its own and applies every
#      migration into it, which costs twenty to thirty seconds per module and is pure
#      setup. Distributing by file rather than by test keeps one module's schema on one
#      worker, so that setup is paid once per module rather than once per test.
#
# Each parallel worker of group three runs against a database of its own. Creating or
# dropping a schema modifies the descriptor of the database that holds it, so workers
# sharing one database contend on one row for every module they start; that contention
# was failing the suites both in parallel and, less often, serially, where one module's
# teardown overlapped the next module's setup.
#
# Usage:
#   scripts/run_tests.sh              the credential-free groups only
#   scripts/run_tests.sh --instance   those, plus the instance-backed suites
#   scripts/run_tests.sh --instance --workers 4
#
# The instance-backed group is opt-in because it needs a reachable cluster. Without
# MOLT_TEST_DSN it would skip rather than fail, and a run that silently skipped the
# suites someone asked for would report a pass that means less than it appears to.

set -o errexit
set -o nounset
set -o pipefail

# The interpreter the suites run under. A virtual environment in the working tree is
# preferred over one on PATH, and both are overridable. The preference is not a
# convenience: an environment under the system temporary directory is removed by the
# operating system's own housekeeping without warning, which has already cost one
# session its whole toolchain mid-run.
# A path holding no space is preferred among the candidates, because the suites write a
# small executable and run it: an interpreter path containing a space cannot be named by
# a plain shebang, and a checkout under such a path has already broken its own tests
# that way.
if [ -n "${MOLT_PYTHON:-}" ]; then
  readonly PYTHON="${MOLT_PYTHON}"
elif [ -x "${HOME}/.molt-venv/bin/python" ]; then
  readonly PYTHON="${HOME}/.molt-venv/bin/python"
elif [ -x ".venv/bin/python" ]; then
  readonly PYTHON="./.venv/bin/python"
else
  readonly PYTHON="python3.12"
fi

if ! "$PYTHON" -c "import pytest" > /dev/null 2>&1; then
  echo "the interpreter ${PYTHON} has no test runner installed." >&2
  echo "Create an environment and install the pinned dependencies:" >&2
  echo "  python3.12 -m venv \"\${HOME}/.molt-venv\"" >&2
  echo "  \"\${HOME}/.molt-venv/bin/python\" -m pip install -r requirements.txt" >&2
  echo "Or point MOLT_PYTHON at an interpreter that already has them." >&2
  exit 2
fi

# The suites the credential-free group is drawn from, gates excluded; the gate suite is
# run on its own below.
readonly PARALLEL_SUITES=(
  tests/unit
  tests/property
  tests/spec
  tests/mcp
  tests/security
  tests/infra
  tests/ci
  tests/skills
)

# The suites the instance-backed group is drawn from. `tests/property` appears in both
# lists on purpose: it holds both kinds. Most properties are in-process, while a handful
# drive a real erasure against a cluster, and those are marked. Splitting the group by
# marker rather than by directory is what keeps each group homogeneous — the first
# genuinely credential-free, the second genuinely cluster-bound.
readonly INSTANCE_SUITES=(
  tests/integration
  tests/concurrency
  tests/property
  tests/e2e
)

# What separates the two groups. A cluster-bound test in the parallel group would run
# eight heavy erasures at once against one instance, which is how three of them were
# seen to fail under load while passing serially; and it would also run, unmarked and
# unskipped, on a checkout with no cluster at all.
readonly CLUSTER_BOUND='instance or integration or concurrency or e2e'
readonly NOT_CLUSTER_BOUND='not instance and not integration and not concurrency and not e2e'

# The cluster-bound cases that must not share the instance, held out of the parallel pass
# and run afterwards on their own. Each measures the contention it creates itself against
# a bounded retry budget, so unrelated load does not make it stricter: the budget is spent
# on the neighbours and the case reports exhaustion where its property holds.
readonly SERIAL_ONLY='serial'

run_instance=0
workers="auto"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --instance) run_instance=1 ;;
    --workers)
      shift
      [ "$#" -gt 0 ] || { echo "--workers needs a count" >&2; exit 2; }
      workers="$1"
      ;;
    -h | --help)
      sed -n '2,40p' "$0"
      exit 0
      ;;
    *)
      echo "unrecognised argument: $1" >&2
      exit 2
      ;;
  esac
  shift
done

# Run the type checker, rebuilding its cache once if the cache is what failed.
#
# The checker keeps an incremental cache in the working tree, and an interrupted or
# overlapping run can leave that cache inconsistent. It then reports an internal error
# on every subsequent run until the cache is discarded, which looks like a type error in
# the source and is not one. Overlapping runs are ordinary here: the gate suite drives
# the same command through a subprocess, so a developer running both at once produces
# exactly that state. Discarding a derived cache is safe, so the retry is automatic and
# says what it did rather than leaving someone to discover the remedy.
run_type_check() {
  local output
  if output="$("$PYTHON" -m mypy 2>&1)"; then
    printf '%s\n' "$output"
    return 0
  fi
  if printf '%s' "$output" | grep -q "INTERNAL ERROR"; then
    echo "the type checker's incremental cache is inconsistent; discarding and retrying"
    rm -rf .mypy_cache
    "$PYTHON" -m mypy
    return
  fi
  printf '%s\n' "$output"
  return 1
}

echo "== static gates (serial: each tool is already parallel inside itself) =="
"$PYTHON" -m ruff check src/molt tests scripts infra
"$PYTHON" -m ruff format --check src/molt tests scripts infra
run_type_check
"$PYTHON" scripts/hygiene.py
"$PYTHON" scripts/check_type_ignores.py

echo
echo "== credential-free suites (${workers} workers) =="
"$PYTHON" -m pytest "${PARALLEL_SUITES[@]}" -q -n "$workers" --dist loadfile \
  -m "$NOT_CLUSTER_BOUND"

echo
echo "== gate suite (serial: it drives the whole-tree tools through a subprocess) =="
"$PYTHON" -m pytest tests/quality -q

if [ "$run_instance" -eq 1 ]; then
  if [ -z "${MOLT_TEST_DSN:-}" ]; then
    echo "MOLT_TEST_DSN is unset, so the instance-backed suites would skip rather" >&2
    echo "than run. Start a local cluster and export it, or drop --instance." >&2
    exit 2
  fi
  echo
  echo "== instance-backed suites (${workers} workers, one database each) =="
  "$PYTHON" -m pytest "${INSTANCE_SUITES[@]}" -q -n "$workers" --dist loadfile \
    -m "($CLUSTER_BOUND) and not $SERIAL_ONLY"

  echo
  echo "== contention cases (serial: each measures the conflict it creates itself) =="
  "$PYTHON" -m pytest "${INSTANCE_SUITES[@]}" -q -m "($CLUSTER_BOUND) and $SERIAL_ONLY"
fi

echo
echo "all requested suites passed"
