#!/usr/bin/env bash
# Publish one verified component result to the CICD status preview branch.

set -Eeuo pipefail

readonly TARGET_RELEASE="6.1.1.7"
readonly TARGET_STREAM="${TARGET_RELEASE}.z"
readonly TARGET_REPOSITORY="noironetworks/cicd-status"
readonly TARGET_BRANCH="${CICD_STATUS_BRANCH:-main}"
readonly TARGET_REPOSITORY_URL="https://github.com/${TARGET_REPOSITORY}.git"
readonly MAX_PUSH_ATTEMPTS=3
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly UPDATER="${SCRIPT_DIR}/update-github-actions-release.py"

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_nonempty() {
  local name=$1
  [[ -n "${!name:-}" ]] || die "${name} is required"
}

[[ $# -eq 2 && "$1" == "--component" ]] ||
  die "usage: $0 --component aci-containers|opflex"
readonly COMPONENT=$2
case "${COMPONENT}" in
  aci-containers)
    readonly SOURCE_REPOSITORY="noironetworks/aci-containers"
    readonly SOURCE_TAG="${TARGET_RELEASE}"
    readonly SOURCE_COMMIT_ENV="ACI_COMMIT"
    readonly RESULT_DESCRIPTION="all eight ACI images"
    COMPONENT_IMAGES=(
      aci-containers-host
      aci-containers-controller
      cnideploy
      aci-containers-operator
      openvswitch
      aci-containers-webhook
      aci-containers-certmanager
      aci-containers-host-ovscni
    )
    ;;
  opflex)
    readonly SOURCE_REPOSITORY="noironetworks/opflex"
    readonly SOURCE_TAG="${TARGET_RELEASE}"
    readonly SOURCE_COMMIT_ENV="OPFLEX_COMMIT"
    readonly RESULT_DESCRIPTION="the final opflex image"
    COMPONENT_IMAGES=(opflex)
    ;;
  *)
    die "component must be aci-containers or opflex"
    ;;
esac
readonly SOURCE_REF="refs/tags/${SOURCE_TAG}"
readonly SECURITY_ARTIFACT_ROOT="${CI_ARTIFACT_DIR}/security-artifacts"

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [[ -n "${STATUS_WORK_DIR:-}" && -d "${STATUS_WORK_DIR}" ]]; then
    rm -rf -- "${STATUS_WORK_DIR}"
  fi
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

[[ "${GITHUB_ACTIONS:-false}" == "true" ]] || die "this publisher is restricted to GitHub Actions"
[[ "${GITHUB_REPOSITORY:-}" == "${SOURCE_REPOSITORY}" ]] || die "unexpected source repository"
[[ "${GITHUB_REF:-}" == "${SOURCE_REF}" ]] || die "unexpected source ref"
[[ "${GITHUB_REF_NAME:-}" == "${SOURCE_TAG}" ]] || die "unexpected source tag"
[[ "${SKIP_CICD_STATUS:-true}" == "false" ]] || die "CICD status publication is disabled"

require_nonempty "${SOURCE_COMMIT_ENV}"
require_nonempty GITHUB_RUN_ID
require_nonempty GITHUB_RUN_ATTEMPT
require_nonempty UPSTREAM_IMAGE_Z_TAG
require_nonempty GHA_DATED_IMAGE_TAG
require_nonempty CICD_COMMIT
require_nonempty CI_ARTIFACT_DIR
require_nonempty TEST_CICD_STATUS_TOKEN
require_nonempty GITHUB_RUN_NUMBER
readonly SOURCE_COMMIT="${!SOURCE_COMMIT_ENV}"

[[ "${SOURCE_COMMIT}" =~ ^[0-9a-f]{40}$ ]] || die "source commit must be a lowercase 40-character Git SHA"
[[ "${CICD_COMMIT}" =~ ^[0-9a-f]{40}$ ]] || die "CICD_COMMIT must be a lowercase 40-character Git SHA"
[[ "${GITHUB_RUN_ID}" =~ ^[1-9][0-9]*$ ]] || die "GITHUB_RUN_ID must be a positive integer"
[[ "${GITHUB_RUN_ATTEMPT}" =~ ^[1-9][0-9]*$ ]] || die "GITHUB_RUN_ATTEMPT must be a positive integer"
[[ "${TEST_CICD_STATUS_TOKEN}" != *$'\n'* && "${TEST_CICD_STATUS_TOKEN}" != *$'\r'* ]] ||
  die "status token contains a newline"
[[ -f "${UPDATER}" && ! -L "${UPDATER}" ]] || die "missing status updater ${UPDATER}"

readonly QUAY_MANIFEST="${CI_ARTIFACT_DIR}/quay-noiro-published-images.tsv"
readonly DOCKER_MANIFEST="${CI_ARTIFACT_DIR}/docker-published-images.tsv"
[[ -f "${QUAY_MANIFEST}" && ! -L "${QUAY_MANIFEST}" ]] || die "missing publication manifest ${QUAY_MANIFEST}"
[[ -f "${DOCKER_MANIFEST}" && ! -L "${DOCKER_MANIFEST}" ]] || die "missing publication manifest ${DOCKER_MANIFEST}"

STATUS_WORK_DIR="$(mktemp -d "${RUNNER_TEMP:-/tmp}/gha-cicd-status.XXXXXXXX")"
readonly STATUS_WORK_DIR
chmod 700 "${STATUS_WORK_DIR}"
readonly ASKPASS_SCRIPT="${STATUS_WORK_DIR}/git-askpass.sh"
cat > "${ASKPASS_SCRIPT}" <<'ASKPASS'
#!/usr/bin/env bash
case "$1" in
  *Username*) printf '%s\n' 'x-access-token' ;;
  *Password*) printf '%s\n' "${TEST_CICD_STATUS_TOKEN:?}" ;;
  *) exit 1 ;;
esac
ASKPASS
chmod 700 "${ASKPASS_SCRIPT}"
export GIT_ASKPASS="${ASKPASS_SCRIPT}"
export GIT_TERMINAL_PROMPT=0
readonly STATUS_TIMESTAMP="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

stage_security_artifacts() {
  local repository_dir=$1
  local image
  local source_dir
  local target_dir
  local cve_path
  local cve_base_path
  local sbom_path

  sanitize_report_copy() {
    local source_path=$1
    local destination_path=$2
    sed -E 's/[[:space:]]+$//' "${source_path}" > "${destination_path}"
  }

  for image in "${COMPONENT_IMAGES[@]}"; do
    source_dir="${SECURITY_ARTIFACT_ROOT}/${image}"
    cve_path="${source_dir}/cve.txt"
    cve_base_path="${source_dir}/cve-base.txt"
    sbom_path="${source_dir}/sbom.txt"
    [[ -f "${cve_path}" && ! -L "${cve_path}" ]] ||
      die "missing scan artifact ${cve_path}"
    [[ -f "${cve_base_path}" && ! -L "${cve_base_path}" ]] ||
      die "missing scan artifact ${cve_base_path}"
    [[ -f "${sbom_path}" && ! -L "${sbom_path}" ]] ||
      die "missing scan artifact ${sbom_path}"

    target_dir="${repository_dir}/docs/release_artifacts/${TARGET_RELEASE}/z/${image}"
    install -d -m 755 "${target_dir}"
    sanitize_report_copy "${cve_path}" "${target_dir}/${TARGET_RELEASE}-cve.txt"
    sanitize_report_copy "${cve_base_path}" "${target_dir}/${TARGET_RELEASE}-cve-base.txt"
    sanitize_report_copy "${sbom_path}" "${target_dir}/${TARGET_RELEASE}-sbom.txt"

    if [[ -f "${source_dir}/base-image-sha.txt" && ! -L "${source_dir}/base-image-sha.txt" ]]; then
      cp "${source_dir}/base-image-sha.txt" "${target_dir}/${TARGET_RELEASE}-base-image-sha.txt"
    fi
  done
}

validate_changed_paths() {
  local repository_dir=$1
  local line
  local path
  local unexpected=()

  while IFS= read -r line; do
    [[ -n "${line}" ]] || continue
    path="${line:3}"
    case "${path}" in
      docs/release_artifacts/releases.yaml)
        ;;
      docs/release_artifacts/${TARGET_RELEASE}/z/*/${TARGET_RELEASE}-cve.txt)
        ;;
      docs/release_artifacts/${TARGET_RELEASE}/z/*/${TARGET_RELEASE}-cve-base.txt)
        ;;
      docs/release_artifacts/${TARGET_RELEASE}/z/*/${TARGET_RELEASE}-sbom.txt)
        ;;
      docs/release_artifacts/${TARGET_RELEASE}/z/*/${TARGET_RELEASE}-base-image-sha.txt)
        ;;
      *)
        unexpected+=("${path}")
        ;;
    esac
  done < <(git -C "${repository_dir}" status --short --untracked-files=all)

  if ((${#unexpected[@]} > 0)); then
    printf 'unexpected file changes:\n%s\n' "${unexpected[*]}" >&2
    die "status updater changed files outside the allowed release artifacts"
  fi
}

for ((attempt = 1; attempt <= MAX_PUSH_ATTEMPTS; attempt++)); do
  attempt_dir="${STATUS_WORK_DIR}/attempt-${attempt}"
  repository_dir="${attempt_dir}/cicd-status-target"
  install -d -m 700 "${attempt_dir}"
  git -c credential.helper= clone --quiet --depth=1 --single-branch \
    --branch "${TARGET_BRANCH}" -- "${TARGET_REPOSITORY_URL}" "${repository_dir}"

  [[ "$(git -C "${repository_dir}" remote get-url origin)" == "${TARGET_REPOSITORY_URL}" ]] ||
    die "cloned repository has an unexpected origin"
  [[ "$(git -C "${repository_dir}" branch --show-current)" == "${TARGET_BRANCH}" ]] ||
    die "cloned repository is on an unexpected branch"
  stage_security_artifacts "${repository_dir}"

  python3 "${UPDATER}" \
    --component "${COMPONENT}" \
    --releases-file "${repository_dir}/docs/release_artifacts/releases.yaml" \
    --manifest "${QUAY_MANIFEST}" \
    --docker-manifest "${DOCKER_MANIFEST}" \
    --image-z-tag "${UPSTREAM_IMAGE_Z_TAG}" \
    --image-dated-tag "${GHA_DATED_IMAGE_TAG}" \
    --source-commit "${SOURCE_COMMIT}" \
    --cicd-commit "${CICD_COMMIT}" \
    --run-id "${GITHUB_RUN_ID}" \
    --run-attempt "${GITHUB_RUN_ATTEMPT}" \
    --run-number "${GITHUB_RUN_NUMBER}" \
    --timestamp "${STATUS_TIMESTAMP}"

  git -C "${repository_dir}" diff --check
  validate_changed_paths "${repository_dir}"

  changed_paths=("docs/release_artifacts/releases.yaml")
  for image in "${COMPONENT_IMAGES[@]}"; do
    changed_paths+=(
      "docs/release_artifacts/${TARGET_RELEASE}/z/${image}/${TARGET_RELEASE}-cve.txt"
      "docs/release_artifacts/${TARGET_RELEASE}/z/${image}/${TARGET_RELEASE}-cve-base.txt"
      "docs/release_artifacts/${TARGET_RELEASE}/z/${image}/${TARGET_RELEASE}-sbom.txt"
    )
    if [[ -f "${repository_dir}/docs/release_artifacts/${TARGET_RELEASE}/z/${image}/${TARGET_RELEASE}-base-image-sha.txt" ]]; then
      changed_paths+=("docs/release_artifacts/${TARGET_RELEASE}/z/${image}/${TARGET_RELEASE}-base-image-sha.txt")
    fi
  done

  if git -C "${repository_dir}" diff --exit-code --quiet -- "${changed_paths[@]}"; then
    echo "The ${TARGET_STREAM} ${COMPONENT} result is already current; nothing to push."
    exit 0
  fi

  git -C "${repository_dir}" config user.name "github-actions-status"
  git -C "${repository_dir}" config user.email "github-actions-status@users.noreply.github.com"
  git -C "${repository_dir}" add -- "${changed_paths[@]}"
  git -C "${repository_dir}" diff --cached --check
  git -C "${repository_dir}" commit --quiet \
    -m "${TARGET_STREAM} ${COMPONENT} run ${GITHUB_RUN_ID}.${GITHUB_RUN_ATTEMPT}" \
    -m "Source: ${SOURCE_COMMIT}" \
    -m "CICD: ${CICD_COMMIT}" \
    -m "Image tags: ${UPSTREAM_IMAGE_Z_TAG}, ${GHA_DATED_IMAGE_TAG}"

  if git -C "${repository_dir}" -c credential.helper= push --quiet origin "HEAD:${TARGET_BRANCH}"; then
    echo "Published ${RESULT_DESCRIPTION} to ${TARGET_REPOSITORY}/${TARGET_BRANCH} (${TARGET_STREAM})."
    exit 0
  fi
  if ((attempt == MAX_PUSH_ATTEMPTS)); then
    die "status push failed after ${MAX_PUSH_ATTEMPTS} fresh-clone attempts"
  fi
  echo "Status branch advanced concurrently; retrying from a fresh clone (${attempt}/${MAX_PUSH_ATTEMPTS})." >&2
done

die "unreachable status publisher state"
