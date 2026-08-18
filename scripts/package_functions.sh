#!/usr/bin/env bash
#
# Build the function deployment package and upload it to the bucket the collector
# and console stacks read their code from, then print the two deployment values
# that name it on standard output.
#
# Both function stacks take a bucket and a key and nothing else: the templates do
# not inspect the archive's contents, only that the object exists when the stack is
# created. That single fact decides the shape of this script. A package holding the
# application package alone, with no dependency tree in it, is enough to create both
# functions, both function endpoints, both roles, both log groups, the checkpoint
# rule, and the distribution in front of the console, which yields a reachable
# demonstration address well before a full package can be produced. Such a stub is
# not a different artefact and gets no branch of its own: it is this same path with
# the dependency step omitted, which is what --without-dependencies does. Staging,
# archiving, naming, and uploading are shared by both, so the archive that carries a
# dependency tree is built, named, and uploaded by exactly the code the stub is
# built, named, and uploaded by, and neither can drift from the other.
#
# The runtime expects the application package and every dependency at the archive
# root, so the staging directory is the archive root: src/molt is copied to
# molt/ within it and dependencies are installed into it rather than beside it.
# Dependencies are resolved for the deployed platform, not for this machine, so the
# archive is the same whichever machine builds it.
#
# Idempotence comes from the key. The derived key carries a digest of the archive's
# own bytes, and the archive is built deterministically, so re-running over an
# unchanged tree derives the same key, finds the object already present, and uploads
# nothing. An explicit --key overrides the derivation for a caller that wants a
# stable name, and is then written unconditionally.
#
# Usage:
#   package_functions.sh --bucket NAME [--prefix PATH] [--key PATH]
#                        [--region NAME] [--without-dependencies] [--dry-run]
#
#   --bucket NAME             bucket the archive is uploaded to
#   --prefix PATH             key prefix the derived key sits under
#   --key PATH                exact key, overriding the derived one
#   --region NAME             region the bucket lives in
#   --without-dependencies    stage the application package alone, the stub case
#   --dry-run                 build and name the archive, upload nothing
#
# No credential is read, passed, or printed by any part of this script. The bucket
# and the key are non-secret deployment values, and standard output carries the two
# of them and nothing else, in the assignment form the deployment command takes, so
# it is safe to capture. Every diagnostic goes to standard error.

set -o errexit
set -o nounset
set -o pipefail

readonly REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly PACKAGE_SOURCE="${REPO_ROOT}/src/molt"

# The console's rendered assets. Not part of the package directory, so they are staged
# separately and at the same place relative to the archive root that they occupy relative
# to the repository root, which is what lets one resolver serve both layouts.
readonly WEB_SOURCE="${REPO_ROOT}/web"

# The Interface_Specification document, and where it sits relative to a checkout's root.
# The console reads it at the configured path resolved against its own root, so the archive
# has to carry it at the same relative place. The default this names is the one the
# configuration surface declares.
readonly SPEC_RELATIVE="docs/interface.json"
readonly SPEC_SOURCE="${REPO_ROOT}/${SPEC_RELATIVE}"
readonly MANIFEST="${REPO_ROOT}/pyproject.toml"
# The interpreter the archive is staged and installed with. It has to be one that holds
# a package installer, which the bare platform interpreter need not, and the order is
# the test runner's and the deployment script's so every tool on one machine resolves
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
readonly AWS_BIN="${MOLT_AWS_BIN:-aws}"

# The deployed runtime and processor family, which the two function templates
# declare and which the dependency resolution is asked for rather than inferred
# from whatever this machine happens to be.
readonly TARGET_PYTHON_VERSION="3.12"

# The compatibility tags the deployed runtime accepts, oldest first. More than one is
# named because a tag is a floor on the platform's own libraries rather than a name for
# it: a package publishing only newer wheels has none that satisfy an older tag, and
# resolution then fails naming the pinned version as though it did not exist. That is
# what happened — the driver's pinned version publishes wheels above the single tag
# named here, so the packaging refused a version that is on the index. The runtime is
# a current distribution whose libraries satisfy both, so admitting both lets each
# dependency resolve to the newest wheel it actually ships while nothing older breaks.
readonly TARGET_PLATFORMS=(
  "manylinux2014_aarch64"
  "manylinux_2_28_aarch64"
)

# The length of the digest the derived key carries. Long enough that two unequal
# archives do not collide, short enough to read in a stack event.
readonly DIGEST_LENGTH=16

bucket=""
key_prefix="functions/"
explicit_key=""
region_argument=()
with_dependencies="true"
dry_run="false"
stage_dir=""

log() {
  printf '%s\n' "$*" >&2
}

die() {
  log "error: $*"
  exit 2
}

usage() {
  cat >&2 <<'USAGE'
Usage:
  package_functions.sh --bucket NAME [--prefix PATH] [--key PATH]
                       [--region NAME] [--without-dependencies] [--dry-run]

  --bucket NAME             bucket the archive is uploaded to
  --prefix PATH             key prefix the derived key sits under
  --key PATH                exact key, overriding the derived one
  --region NAME             region the bucket lives in
  --without-dependencies    stage the application package alone, the stub case
  --dry-run                 build and name the archive, upload nothing
  --help                    print this block

Standard output carries the two deployment values, one assignment per line, in the
form the deployment command takes. Every diagnostic goes to standard error and no
credential is read, passed, or printed.
USAGE
}

parse_arguments() {
  while (($# > 0)); do
    case "$1" in
      --bucket)
        (($# >= 2)) || die "--bucket takes a bucket name"
        bucket="$2"
        shift 2
        ;;
      --prefix)
        (($# >= 2)) || die "--prefix takes a key prefix"
        key_prefix="$2"
        shift 2
        ;;
      --key)
        (($# >= 2)) || die "--key takes an object key"
        explicit_key="$2"
        shift 2
        ;;
      --region)
        (($# >= 2)) || die "--region takes a region name"
        region_argument=(--region "$2")
        shift 2
        ;;
      --without-dependencies)
        with_dependencies="false"
        shift
        ;;
      --dry-run)
        dry_run="true"
        shift
        ;;
      --help)
        usage
        exit 0
        ;;
      *)
        die "unknown argument '$1'"
        ;;
    esac
  done
  [[ -n "${bucket}" ]] || {
    usage
    die "--bucket is required"
  }
  [[ "${key_prefix}" != /* ]] || die "the key prefix carries no leading separator"
  [[ -d "${PACKAGE_SOURCE}" ]] || die "no package source at ${PACKAGE_SOURCE}"
  [[ -d "${WEB_SOURCE}" ]] || die "no console assets at ${WEB_SOURCE}"
}

require_binaries() {
  command -v "${PYTHON}" >/dev/null 2>&1 || die "${PYTHON} is not on PATH"
  if [[ "${with_dependencies}" == "true" ]]; then
    "${PYTHON}" -m pip --version >/dev/null 2>&1 ||
      die "${PYTHON} has no package installer, which the dependency step needs"
  fi
  if [[ "${dry_run}" != "true" ]]; then
    command -v "${AWS_BIN}" >/dev/null 2>&1 || die "${AWS_BIN} is not on PATH"
  fi
}

cleanup() {
  [[ -z "${stage_dir}" ]] || rm -rf -- "${stage_dir}"
}

# The staging directory is the archive root. Compiled caches are left behind
# because they are keyed to the building machine's interpreter and the deployed one
# ignores them, so copying them would only make two builds of one tree differ.
stage_package() {
  stage_dir="$(mktemp -d)"
  trap cleanup EXIT
  mkdir -p -- "${stage_dir}/root"
  "${PYTHON}" - "${PACKAGE_SOURCE}" "${stage_dir}/root/molt" <<'PY'
import shutil
import sys
from pathlib import Path

source, destination = (Path(argument) for argument in sys.argv[1:3])
shutil.copytree(
    source,
    destination,
    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
)
PY
  log "staged the application package"
  # The console's templates and stylesheet, staged beside the package rather than inside
  # it, at the same place relative to the archive root that they occupy relative to a
  # checkout's root. The console resolves them from its own module location, so one
  # layout serves both.
  #
  # They were absent, and every page of the console answered that its templates were
  # unavailable. The package alone is enough to create every resource of both function
  # stacks, so nothing about the deployment complained: the archive existed, the function
  # was created, the handler imported, and the failure arrived per request as a rendering
  # fault. An archive that omits them is not a smaller archive, it is a console with no
  # page that answers.
  "${PYTHON}" - "${WEB_SOURCE}" "${stage_dir}/root/web" <<'PY'
import shutil
import sys
from pathlib import Path

source, destination = (Path(argument) for argument in sys.argv[1:3])
if not source.is_dir():
    sys.stderr.write(f"package: no web assets at {source}\n")
    raise SystemExit(2)
shutil.copytree(
    source,
    destination,
    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
)
PY
  log "staged the console's templates and stylesheet"
  # The Interface_Specification document, staged at the same place relative to the archive
  # root that it occupies relative to a checkout's, for the same reason and by the same
  # resolution as the templates above.
  #
  # It was absent, and the route that serves it answered 503 on every request while the
  # footer of every page linked to it. This is the same omission as the templates in a
  # second place: the deployment created every resource, the handler imported, and the only
  # symptom was one route failing per request. A document that describes the shapes this
  # console serves is not optional to the console — a reviewer following the link is
  # entitled to the thing it points at.
  "${PYTHON}" - "${SPEC_SOURCE}" "${stage_dir}/root/${SPEC_RELATIVE}" <<'PY'
import shutil
import sys
from pathlib import Path

source, destination = (Path(argument) for argument in sys.argv[1:3])
if not source.is_file():
    sys.stderr.write(f"package: no interface specification at {source}\n")
    raise SystemExit(2)
destination.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(source, destination)
PY
  log "staged the interface specification document"
}

# The direct runtime dependencies, read off the project manifest so the pins the
# archive carries are the pins the manifest states rather than a second list that
# can fall behind it. The verification tooling is an optional group and is not read
# here, so none of it reaches the archive.
runtime_requirements() {
  "${PYTHON}" - "${MANIFEST}" <<'PY'
import sys
import tomllib
from pathlib import Path

document = tomllib.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
project = document.get("project", {})
for requirement in project.get("dependencies", []):
    sys.stdout.write(f"{requirement}\n")
PY
}

# The one step the stub omits. Everything before and after it is shared, so the two
# archives differ in their contents and in nothing else about how they are produced.
install_dependencies() {
  if [[ "${with_dependencies}" != "true" ]]; then
    log "omitting the dependency tree; the archive is the stub the templates accept"
    return 0
  fi
  local -a requirements=()
  local line
  while IFS= read -r line; do
    [[ -n "${line}" ]] || continue
    requirements+=("${line}")
  done < <(runtime_requirements)
  ((${#requirements[@]} > 0)) || die "the project manifest states no runtime dependency"
  local -a platforms=()
  local tag
  for tag in "${TARGET_PLATFORMS[@]}"; do
    platforms+=(--platform "${tag}")
  done
  log "installing ${#requirements[@]} runtime dependencies for the deployed platform"
  "${PYTHON}" -m pip install \
    --quiet \
    --target "${stage_dir}/root" \
    --implementation cp \
    --python-version "${TARGET_PYTHON_VERSION}" \
    "${platforms[@]}" \
    --only-binary :all: \
    --upgrade \
    "${requirements[@]}" >&2
}

# Entries are written in sorted order under one fixed modification stamp, so two
# builds of one tree produce byte-identical archives and the digest below names the
# contents rather than the moment of the build.
build_archive() {
  "${PYTHON}" - "${stage_dir}/root" "${stage_dir}/package.zip" <<'PY'
import sys
import zipfile
from pathlib import Path

root, archive = (Path(argument) for argument in sys.argv[1:3])
FIXED_STAMP = (1980, 1, 1, 0, 0, 0)
EXECUTABLE_BIT = 0o111
with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        entry = zipfile.ZipInfo(
            path.relative_to(root).as_posix(),
            date_time=FIXED_STAMP,
        )
        entry.compress_type = zipfile.ZIP_DEFLATED
        mode = 0o755 if path.stat().st_mode & EXECUTABLE_BIT else 0o644
        entry.external_attr = mode << 16
        bundle.writestr(entry, path.read_bytes())
PY
  log "archive built"
}

archive_digest() {
  "${PYTHON}" - "${stage_dir}/package.zip" "${DIGEST_LENGTH}" <<'PY'
import hashlib
import sys
from pathlib import Path

path, length = Path(sys.argv[1]), int(sys.argv[2])
digest = hashlib.sha256(path.read_bytes()).hexdigest()
sys.stdout.write(digest[:length])
PY
}

# The derived key names the archive by its own bytes, which is what makes a second
# run over an unchanged tree a no-op rather than a re-upload.
compose_key() {
  if [[ -n "${explicit_key}" ]]; then
    printf '%s' "${explicit_key}"
    return 0
  fi
  printf '%smolt-%s.zip' "${key_prefix}" "$(archive_digest)"
}

object_exists() {
  "${AWS_BIN}" s3api head-object \
    ${region_argument[@]+"${region_argument[@]}"} \
    --bucket "${bucket}" \
    --key "$1" \
    >/dev/null 2>&1
}

upload_archive() {
  local key="$1"
  if [[ -z "${explicit_key}" ]] && object_exists "${key}"; then
    log "the bucket already holds this archive under ${key}, uploading nothing"
    return 0
  fi
  "${AWS_BIN}" s3api put-object \
    ${region_argument[@]+"${region_argument[@]}"} \
    --bucket "${bucket}" \
    --key "${key}" \
    --body "${stage_dir}/package.zip" \
    >/dev/null
  log "uploaded the archive under ${key}"
}

main() {
  parse_arguments "$@"
  require_binaries
  stage_package
  install_dependencies
  build_archive
  local key
  key="$(compose_key)"
  if [[ "${dry_run}" == "true" ]]; then
    log "archive named ${key}, uploading nothing"
  else
    upload_archive "${key}"
  fi
  printf 'CodeBucket=%s\n' "${bucket}"
  printf 'CodeKey=%s\n' "${key}"
}

main "$@"
