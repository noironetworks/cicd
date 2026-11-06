#!/bin/bash
set -Eeuo pipefail
if [[ "${GITHUB_ACTIONS:-false}" != "true" ]]; then
    set -x
fi
SCRIPTS_DIR=$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPTS_DIR/globals.sh"

if [[ ${TRAVIS_REPO_SLUG##*/} != "acc-provision" ]]; then
    IMAGE_BUILD_REGISTRY=$1
    IMAGE=$2
    IMAGE_BUILD_TAG=$3
    OTHER_IMAGE_TAGS=$4
    IMAGE_SHA=$5
    BASE_IMAGE=$6
else
    PYPI_REGISTRY=$1
    TAG_NAME=$2
    IS_RELEASE=$3
    TWINE_UPLOAD=$4
fi

GIT_REPO=${CICD_STATUS_REPO}
GIT_BRANCH="main"
GIT_USER="travis-tagger"
: "${TRAVIS_TAGGER:?TRAVIS_TAGGER is required}"

STATUS_WORK_DIR=$(mktemp -d /tmp/cicd-status-publish.XXXXXXXX)
trap 'rm -rf -- "${STATUS_WORK_DIR}"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
cat > "${STATUS_WORK_DIR}/git-askpass.sh" <<'ASKPASS'
#!/bin/bash
case "$1" in
    *Username*) printf '%s\n' 'x-access-token' ;;
    *Password*) printf '%s\n' "${TRAVIS_TAGGER:?}" ;;
    *) exit 1 ;;
esac
ASKPASS
chmod 700 "${STATUS_WORK_DIR}/git-askpass.sh"
export GIT_ASKPASS="${STATUS_WORK_DIR}/git-askpass.sh"
export GIT_TERMINAL_PROMPT=0

write_build_log() {
    local destination=$1
    if [[ "${GITHUB_ACTIONS:-false}" == "true" ]]; then
        {
            printf 'GitHub Actions run: %s\n' "${TRAVIS_JOB_WEB_URL}"
            printf 'Source commit: %s\n' "${TRAVIS_COMMIT}"
            printf 'Trigger tag: %s\n' "${TRAVIS_TAG}"
        } > "${destination}"
    else
        curl --fail --show-error --silent \
            "https://api.travis-ci.com/v3/job/${TRAVIS_JOB_ID}/log.txt" > "${destination}"
        sed -i '/X-Amz-.*=\([^&]*\)&/d' "${destination}"
    fi
}

TG=${RELEASE_Z_TAG}
if [[ ${TRAVIS_REPO_SLUG##*/} == "acc-provision" ]]; then
    STREAM=z
    if [[ "${IS_RELEASE}" == "true" ]]; then
        STREAM=r
        TG=${RELEASE_TAG}
    elif [[ "$TRAVIS_TAG" =~ $RC_REGEX ]]; then
        RC_NUM=${TRAVIS_TAG#"$RC_PREFIX"}
        STREAM=rc${RC_NUM}
        TG=${RELEASE_TAG}rc${RC_NUM}
    fi
    COMMIT_MESSAGE="${TG}-acc-provision-${TRAVIS_BUILD_NUMBER}"
else
    COMMIT_MESSAGE="${TG}-${IMAGE}-${TRAVIS_BUILD_NUMBER}"
fi

# Reapply this result to a fresh checkout if another component publishes first.
for attempt in 1 2 3; do
    export CICD_STATUS_REPO_DIR="${STATUS_WORK_DIR##*/}/attempt-${attempt}"
    repository_dir="/tmp/${CICD_STATUS_REPO_DIR}"
    git -c credential.helper= clone --quiet --single-branch --branch "${GIT_BRANCH}" \
        "${GIT_REPO}" "${repository_dir}"
    cd "${repository_dir}"
    original_head=$(git rev-parse HEAD)

    if [[ ${TRAVIS_REPO_SLUG##*/} != "acc-provision" ]]; then
        artifact_dir="docs/release_artifacts/${RELEASE_TAG}/z/${IMAGE}"
        mkdir -p "${artifact_dir}"
        write_build_log "${artifact_dir}/${RELEASE_TAG}-buildlog.txt"
        cp /tmp/sbom.txt "${artifact_dir}/${RELEASE_TAG}-sbom.txt"
        cp /tmp/cve.txt "${artifact_dir}/${RELEASE_TAG}-cve.txt"
        cp /tmp/cve-base.txt "${artifact_dir}/${RELEASE_TAG}-cve-base.txt"
        python3 "$SCRIPTS_DIR/update-release.py" "${IMAGE_BUILD_REGISTRY}" "${IMAGE}" \
            "${IMAGE_BUILD_TAG}" "${OTHER_IMAGE_TAGS}" "${IMAGE_SHA}" "${IMAGE_Z_TAG}" \
            "${TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER}" "${BASE_IMAGE}"
        if [[ ${TRAVIS_REPO_SLUG##*/} != "opflex" ]]; then
            trivy image "${IMAGE_BUILD_REGISTRY}/${IMAGE}:${IMAGE_Z_TAG}" \
                >> "${artifact_dir}/${RELEASE_TAG}-cve.txt"
        fi
    else
        artifact_dir="docs/release_artifacts/${RELEASE_TAG}/${STREAM}/acc-provision"
        mkdir -p "${artifact_dir}"
        if [[ "${TWINE_UPLOAD}" == "true" ]]; then
            write_build_log "${artifact_dir}/${RELEASE_TAG}-buildlog.txt"
        fi
        python3 "$SCRIPTS_DIR/update-release.py" "${PYPI_REGISTRY}" "${TAG_NAME}" "${IS_RELEASE}"
    fi

    git config user.email "test@cisco.com"
    git config user.name "${GIT_USER}"
    git add -- docs/release_artifacts
    if [[ ${TRAVIS_REPO_SLUG##*/} != "acc-provision" ]]; then
        git commit --quiet -m "${COMMIT_MESSAGE}-$(date '+%F_%H:%M:%S')" \
            -m "Commit: ${TRAVIS_COMMIT}" \
            -m "Tags: ${IMAGE_Z_TAG}, ${TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER}" \
            -m "ImageId: ${IMAGE_SHA}"
    else
        git commit --quiet -m "${COMMIT_MESSAGE}-$(date '+%F_%H:%M:%S')" \
            -m "Commit: ${TRAVIS_COMMIT}" -m "Tag: ${TAG_NAME}"
    fi
    if git -c credential.helper= push origin "HEAD:${GIT_BRANCH}"; then
        exit 0
    fi
    remote_head=$(git -c credential.helper= ls-remote origin "refs/heads/${GIT_BRANCH}" | cut -f1)
    if [[ "${remote_head}" == "${original_head}" || "${attempt}" == 3 ]]; then
        echo "Error: CICD status push failed (attempt ${attempt})." >&2
        exit 1
    fi
    echo "CICD status advanced concurrently; retrying publication (${attempt}/3)." >&2
done
