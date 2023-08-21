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
else
    PYPI_REGISTRY=$1
    TAG_NAME=$2
fi

GIT_REPO="https://github.com/noironetworks/cicd-status.git"
GIT_LOCAL_DIR="cicd-status"
GIT_BRANCH="main"
GIT_EMAIL="test@cisco.com"
GIT_TOKEN=${TRAVIS_TAGGER}
GIT_USER="travis-tagger"

git_clone_repo() {
    cd /tmp/ || exit
    git clone "${GIT_REPO/:\/\//://$GIT_USER:$GIT_TOKEN@}" "${GIT_LOCAL_DIR}"
    cd /tmp/"${GIT_LOCAL_DIR}" || exit
    git remote set-url origin "${GIT_REPO/:\/\//://$GIT_USER:$GIT_TOKEN@}"
}


add_artifacts() {
    cd /tmp/"${GIT_LOCAL_DIR}" || exit
    git pull --rebase origin ${GIT_BRANCH}
    mkdir -p /tmp/"${GIT_LOCAL_DIR}"/docs/release_artifacts/"${RELEASE_TAG}"/"${IMAGE}" 2> /dev/null
    curl "https://api.travis-ci.com/v3/job/${TRAVIS_JOB_ID}/log.txt" > /tmp/"${GIT_LOCAL_DIR}"/docs/release_artifacts/"${RELEASE_TAG}"/"${IMAGE}"/"${RELEASE_TAG}"-buildlog.txt
    cp /tmp/sbom.txt /tmp/"${GIT_LOCAL_DIR}"/docs/release_artifacts/"${RELEASE_TAG}"/"${IMAGE}"/"${RELEASE_TAG}"-sbom.txt
    cp /tmp/cve.txt /tmp/"${GIT_LOCAL_DIR}"/docs/release_artifacts/"${RELEASE_TAG}"/"${IMAGE}"/"${RELEASE_TAG}"-cve.txt
}

update_container_release() {
    python $SCRIPTS_DIR/update-release.py "${IMAGE_BUILD_REGISTRY}" "${IMAGE}" "${IMAGE_BUILD_TAG}" "${OTHER_IMAGE_TAGS}" "${IMAGE_SHA}" "${IMAGE_Z_TAG}" "${TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER}"
}

add_acc_provision_artifacts() {
    cd /tmp/"${GIT_LOCAL_DIR}" || exit
    git pull --rebase origin ${GIT_BRANCH}
    mkdir -p /tmp/"${GIT_LOCAL_DIR}"/docs/release_artifacts/"${RELEASE_TAG}"/"${IMAGE}" 2> /dev/null
    curl "https://api.travis-ci.com/v3/job/${TRAVIS_JOB_ID}/log.txt" > /tmp/"${GIT_LOCAL_DIR}"/docs/release_artifacts/"${RELEASE_TAG}"/"${TRAVIS_REPO_SLUG##*/}"/"${RELEASE_TAG}"-buildlog.txt
}

update_acc_provision_release(){
    python $SCRIPTS_DIR/update-release.py "${PYPI_REGISTRY}" "${TAG_NAME}"
}

git_add_commit_push() {
    cd /tmp/"${GIT_LOCAL_DIR}" || exit
    git config --local user.email "${GIT_EMAIL}"
    git config --local user.name "${GIT_USER}"
    git stash save --include-untracked
    git pull --rebase origin ${GIT_BRANCH}
    git stash apply
    git add .
    if [[ ${TRAVIS_REPO_SLUG##*/} != "acc-provision" ]]; then
        git commit -a -m "${RELEASE_TAG}-${IMAGE}-${TRAVIS_BUILD_NUMBER}-$(date '+%F_%H:%M:%S')" -m "Commit: ${TRAVIS_COMMIT}" -m "Tags: ${IMAGE_Z_TAG}, ${TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER}" -m "${IMAGE_SHA}"
    else
        git commit -a -m "${RELEASE_TAG}-${TRAVIS_REPO_SLUG##*/}-${TRAVIS_BUILD_NUMBER}-$(date '+%F_%H:%M:%S')" -m "Commit: ${TRAVIS_COMMIT}" -m "Tag: ${TAG_NAME}"
    fi
    git push origin ${GIT_BRANCH}
}


git_clone_repo

if [[ ${TRAVIS_REPO_SLUG##*/} != "acc-provision" ]]; then
    add_artifacts
    update_container_release
else
    add_acc_provision_artifacts
    update_acc_provision_release
fi

git_add_commit_push

