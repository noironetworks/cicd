#!/bin/bash
set -x
SCRIPTS_DIR=$(dirname ${BASH_SOURCE[0]})
source "$SCRIPTS_DIR/globals.sh"

# Get the current branch name
current_branch=$(git rev-parse --abbrev-ref HEAD)

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
GIT_LOCAL_DIR="cicd-status"
GIT_BRANCH="main"
GIT_EMAIL="test@cisco.com"
GIT_TOKEN=${TRAVIS_TAGGER}
GIT_USER="travis-tagger"

git_clone_repo() {
    cd /tmp/ || exit
    git clone "${GIT_REPO/:\/\//://$GIT_USER:$GIT_TOKEN@}" -b "${GIT_BRANCH}" "${GIT_LOCAL_DIR}"
    cd /tmp/"${GIT_LOCAL_DIR}" || exit
    git remote set-url origin "${GIT_REPO/:\/\//://$GIT_USER:$GIT_TOKEN@}"
}

add_artifacts() {
    cd /tmp/"${GIT_LOCAL_DIR}" || exit
    git pull --rebase origin ${GIT_BRANCH}
    mkdir -p /tmp/"${GIT_LOCAL_DIR}"/docs/release_artifacts/"${RELEASE_TAG}"/z/"${IMAGE}" 2> /dev/null
    curl "https://api.travis-ci.com/v3/job/${TRAVIS_JOB_ID}/log.txt" > /tmp/"${GIT_LOCAL_DIR}"/docs/release_artifacts/"${RELEASE_TAG}"/z/"${IMAGE}"/"${RELEASE_TAG}"-buildlog.txt
    sed -i '/X-Amz-.*=\([^&]*\)&/d' /tmp/"${GIT_LOCAL_DIR}"/docs/release_artifacts/"${RELEASE_TAG}"/z/"${IMAGE}"/"${RELEASE_TAG}"-buildlog.txt
    cp /tmp/sbom.txt /tmp/"${GIT_LOCAL_DIR}"/docs/release_artifacts/"${RELEASE_TAG}"/z/"${IMAGE}"/"${RELEASE_TAG}"-sbom.txt
    cp /tmp/cve.txt /tmp/"${GIT_LOCAL_DIR}"/docs/release_artifacts/"${RELEASE_TAG}"/z/"${IMAGE}"/"${RELEASE_TAG}"-cve.txt
    cp /tmp/cve-base.txt /tmp/"${GIT_LOCAL_DIR}"/docs/release_artifacts/"${RELEASE_TAG}"/z/"${IMAGE}"/"${RELEASE_TAG}"-cve-base.txt
}

add_trivy_vulnerabilites() {
    trivy image ${IMAGE_BUILD_REGISTRY}/${IMAGE}:${IMAGE_Z_TAG} >> /tmp/"${GIT_LOCAL_DIR}"/docs/release_artifacts/"${RELEASE_TAG}"/z/"${IMAGE}"/"${RELEASE_TAG}"-cve.txt
}

update_container_release() {

    # Removing local docker image to clear local REPO_DIGEST history. Will be fetching latest digest from upstream
    docker rmi -f "${IMAGE_SHA}"
    python $SCRIPTS_DIR/update-release.py "${IMAGE_BUILD_REGISTRY}" "${IMAGE}" "${IMAGE_BUILD_TAG}" "${OTHER_IMAGE_TAGS}" "${IMAGE_SHA}" "${IMAGE_Z_TAG}" "${TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER}" "${BASE_IMAGE}"
}

add_acc_provision_artifacts() {
    cd /tmp/"${GIT_LOCAL_DIR}" || exit
    git pull --rebase origin ${GIT_BRANCH}
    mkdir -p $1 2> /dev/null
    if [[ ${TWINE_UPLOAD} == "true" ]]; then
        echo "uploading buildlog"
        curl "https://api.travis-ci.com/v3/job/${TRAVIS_JOB_ID}/log.txt" > $1"/"${RELEASE_TAG}-buildlog.txt
    fi
}

update_acc_provision_release() {
    python $SCRIPTS_DIR/update-release.py "${PYPI_REGISTRY}" "${TAG_NAME}" "${IS_RELEASE}"
}

git_add_commit_push() {
    cd /tmp/"${GIT_LOCAL_DIR}" || exit
    git config --local user.email "${GIT_EMAIL}"
    git config --local user.name "${GIT_USER}"
    git stash
    git pull --rebase origin ${GIT_BRANCH}
    if ! git stash pop; then
        echo "Error: Failed to apply stash."
        return 1  # Return failure status
    fi
    git add .
    if [[ ${TRAVIS_REPO_SLUG##*/} != "acc-provision" ]]; then
        DOCKER_REPO_DIGEST_SHA=$(docker image inspect --format '{{index (split (index .RepoDigests 0) "@sha256:") 1}}' ${DOCKER_REGISTRY}/${IMAGE}:${IMAGE_Z_TAG})
        QUAY_REPO_DIGEST_SHA=$(docker image inspect --format '{{index (split (index .RepoDigests 1) "@sha256:") 1}}' ${QUAY_NOIRO_REGISTRY}/${IMAGE}:${IMAGE_Z_TAG})
        git commit -a -m "${RELEASE_Z_TAG}-${IMAGE}-${TRAVIS_BUILD_NUMBER}-$(date '+%F_%H:%M:%S')" -m "Commit: ${TRAVIS_COMMIT}" -m "Tags: ${IMAGE_Z_TAG}, ${TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER}" -m "ImageId: ${IMAGE_SHA}" -m "DockerSha: ${DOCKER_REPO_DIGEST_SHA}" -m "QuaySha: ${QUAY_REPO_DIGEST_SHA}"
    else
        TG=${RELEASE_TAG}
        if [[ "$TRAVIS_TAG" =~ $RC_REGEX ]]; then
            RC_NUM=$(echo "$TRAVIS_TAG" | sed "s/${RC_PREFIX}//")
            TG=${RELEASE_TAG}rc${RC_NUM}
        elif [[ ${IS_RELEASE} == "false" ]] ; then
            TG=${RELEASE_Z_TAG}
        fi
        git commit -a -m "${TG}-${TRAVIS_REPO_SLUG##*/}-${TRAVIS_BUILD_NUMBER}-$(date '+%F_%H:%M:%S')" -m "Commit: ${TRAVIS_COMMIT}" -m "Tag: ${TAG_NAME}"
    fi
    git push origin ${GIT_BRANCH}
}


git_clone_repo

while true; do
    if [[ ${TRAVIS_REPO_SLUG##*/} != "acc-provision" ]]; then
        if ! add_artifacts; then
            break
        fi

        if ! update_container_release; then
            break
        fi

        if [[ ${TRAVIS_REPO_SLUG##*/} != "opflex" ]]; then
            if ! add_trivy_vulnerabilites; then
                break
            fi
        fi
    else
        DIR="/tmp/${GIT_LOCAL_DIR}/docs/release_artifacts/${RELEASE_TAG}/z/${TRAVIS_REPO_SLUG##*/}"
        if [[ "${IS_RELEASE}" == "true" ]]; then
            DIR="/tmp/${GIT_LOCAL_DIR}/docs/release_artifacts/${RELEASE_TAG}/r/${TRAVIS_REPO_SLUG##*/}"
        elif [[ "$TRAVIS_TAG" =~ $RC_REGEX ]]; then
            RC_NUM=$(echo "$TRAVIS_TAG" | sed "s/${RC_PREFIX}//")
            DIR="/tmp/${GIT_LOCAL_DIR}/docs/release_artifacts/${RELEASE_TAG}/rc${RC_NUM}/${TRAVIS_REPO_SLUG##*/}"
        fi

        if ! add_acc_provision_artifacts $DIR; then
            break
        fi

        if ! update_acc_provision_release; then
            break
        fi

    fi


    if git_add_commit_push; then
        break  # Exit the loop if git_add_commit_push succeeds
    else
        # Handle the case when git_add_commit_push fails (e.g., stash pop failed). Resetting and recalculating
        git checkout .
        git pull --rebase origin ${GIT_BRANCH}
        echo "Retrying git_add_commit_push after stash pop failure..."
    fi
done



