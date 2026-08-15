#!/usr/bin/env bash
# Publish one verified acc-provision result to the CICD status preview branch.

set -Eeuo pipefail

readonly TARGET_REPOSITORY="noironetworks/cicd-status"
readonly TARGET_REPOSITORY_URL="https://github.com/${TARGET_REPOSITORY}.git"
readonly TARGET_BRANCH="${CICD_STATUS_BRANCH:-main}"
readonly TARGET_RELEASE="6.1.1.7"
readonly TARGET_STREAM="${TARGET_RELEASE}.z"
readonly WORK_REPO_DIR_NAME="cicd-status"
readonly SOURCE_REPOSITORY="noironetworks/acc-provision"
readonly SOURCE_REF="refs/tags/${TARGET_RELEASE}"
readonly SOURCE_TAG="${TARGET_RELEASE}"
readonly MAX_PUSH_ATTEMPTS=3
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly UPDATER="${SCRIPT_DIR}/update-release.py"

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_nonempty() {
  local name=$1
  [[ -n "${!name:-}" ]] || die "${name} is required"
}

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

require_nonempty TEST_CICD_STATUS_TOKEN
require_nonempty RELEASE_TAG
require_nonempty TRAVIS_TAG
require_nonempty TRAVIS_BUILD_NUMBER
require_nonempty TRAVIS_COMMIT
require_nonempty TRAVIS_REPO_SLUG

export CICD_STATUS_REPO_DIR="${WORK_REPO_DIR_NAME}"

[[ "${TRAVIS_COMMIT}" =~ ^[0-9a-f]{40}$ ]] || die "TRAVIS_COMMIT must be a lowercase 40-character Git SHA"
[[ "${TRAVIS_BUILD_NUMBER}" =~ ^[1-9][0-9]*$ ]] || die "TRAVIS_BUILD_NUMBER must be a positive integer"
[[ "${TEST_CICD_STATUS_TOKEN}" != *$'\n'* && "${TEST_CICD_STATUS_TOKEN}" != *$'\r'* ]] ||
  die "status token contains a newline"
[[ -f "${UPDATER}" && ! -L "${UPDATER}" ]] || die "missing updater ${UPDATER}"

STATUS_WORK_DIR="$(mktemp -d "${RUNNER_TEMP:-/tmp}/acc-provision-cicd-status.XXXXXXXX")"
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

readonly DEV_TAG_NAME="${RELEASE_TAG}.dev${TRAVIS_BUILD_NUMBER}"
readonly PYPI_REGISTRY="https://test.pypi.org/project/acc-provision/${DEV_TAG_NAME}/#files"

for ((attempt = 1; attempt <= MAX_PUSH_ATTEMPTS; attempt++)); do
  attempt_dir="${STATUS_WORK_DIR}/attempt-${attempt}"
  repository_dir="${attempt_dir}/${WORK_REPO_DIR_NAME}"
  install -d -m 700 "${attempt_dir}"

  git -c credential.helper= clone --quiet --depth=1 --single-branch \
    --branch "${TARGET_BRANCH}" -- "${TARGET_REPOSITORY_URL}" "${repository_dir}"

  [[ "$(git -C "${repository_dir}" remote get-url origin)" == "${TARGET_REPOSITORY_URL}" ]] ||
    die "cloned repository has an unexpected origin"
  [[ "$(git -C "${repository_dir}" branch --show-current)" == "${TARGET_BRANCH}" ]] ||
    die "cloned repository is on an unexpected branch"

  rm -rf -- "/tmp/${CICD_STATUS_REPO_DIR}"
  cp -a "${repository_dir}" "/tmp/${CICD_STATUS_REPO_DIR}"

  python3 "${UPDATER}" "${PYPI_REGISTRY}" "${DEV_TAG_NAME}" "false"

  cp "/tmp/${CICD_STATUS_REPO_DIR}/docs/release_artifacts/releases.yaml" \
    "${repository_dir}/docs/release_artifacts/releases.yaml"

  git -C "${repository_dir}" diff --check
  if git -C "${repository_dir}" diff --exit-code --quiet -- docs/release_artifacts/releases.yaml; then
    echo "The ${TARGET_STREAM} acc-provision result is already current; nothing to push."
    exit 0
  fi

  [[ "$(git -C "${repository_dir}" status --short --untracked-files=all)" == \
    ' M docs/release_artifacts/releases.yaml' ]] ||
    die "status updater changed files outside docs/release_artifacts/releases.yaml"

  git -C "${repository_dir}" config user.name "github-actions-status"
  git -C "${repository_dir}" config user.email "github-actions-status@users.noreply.github.com"
  git -C "${repository_dir}" add -- docs/release_artifacts/releases.yaml
  git -C "${repository_dir}" diff --cached --check
  git -C "${repository_dir}" commit --quiet \
    -m "${TARGET_STREAM} acc-provision run ${GITHUB_RUN_ID}.${GITHUB_RUN_ATTEMPT}" \
    -m "Source: ${TRAVIS_COMMIT}" \
    -m "Tag: ${DEV_TAG_NAME}"

  if git -C "${repository_dir}" -c credential.helper= push --quiet origin "HEAD:${TARGET_BRANCH}"; then
    echo "Published acc-provision result to ${TARGET_REPOSITORY}/${TARGET_BRANCH} (${TARGET_STREAM})."
    exit 0
  fi

  if ((attempt == MAX_PUSH_ATTEMPTS)); then
    die "status push failed after ${MAX_PUSH_ATTEMPTS} fresh-clone attempts"
  fi
  echo "Status branch advanced concurrently; retrying from a fresh clone (${attempt}/${MAX_PUSH_ATTEMPTS})." >&2
done

die "unreachable status publisher state"
