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
fi

GIT_REPO="https://github.com/noironetworks/cicd-status.git"
GIT_LOCAL_DIR="cicd-status"
GIT_BRANCH="test"
GIT_EMAIL="test@cisco.com"
GIT_TOKEN=${TRAVIS_TAGGER}
GIT_USER="travis-tagger"

LOCK_TAG="lock-tag"
WAIT_INTERVAL=5  # Adjust this interval (in seconds) to your preference

# Function to check if the lock tag exists remotely
is_locked() {
  git fetch --tags "$GIT_REPO" &>/dev/null
  [ $(git tag -l "$LOCK_TAG" | wc -l) -gt 0 ]
}

# Function to acquire the lock and inform the remote repository
acquire_lock() {
  # Check if the lock tag exists remotely
  if ! is_locked; then
    # Create the lock tag and push it to the remote repository
    git tag "$LOCK_TAG"
    git push --tags "$GIT_REPO"
  fi
}

# Function to release the lock and inform the remote repository
release_lock() {
  # Check if the lock tag exists remotely
  if is_locked; then
    # Delete the lock tag locally and push the deletion to the remote repository
    git tag -d "$LOCK_TAG"
    git push --delete "$GIT_REPO" "$LOCK_TAG"
  fi
}


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
    cp /tmp/cve-base.txt /tmp/"${GIT_LOCAL_DIR}"/docs/release_artifacts/"${RELEASE_TAG}"/"${IMAGE}"/"${RELEASE_TAG}"-cve-base.txt
}

add_trivy_vulnerabilites() {
    trivy image ${IMAGE_BUILD_REGISTRY}/${IMAGE}:${IMAGE_Z_TAG} >> /tmp/"${GIT_LOCAL_DIR}"/docs/release_artifacts/"${RELEASE_TAG}"/"${IMAGE}"/"${RELEASE_TAG}"-cve.txt
}

update_container_release() {
    python $SCRIPTS_DIR/update-release.py "${IMAGE_BUILD_REGISTRY}" "${IMAGE}" "${IMAGE_BUILD_TAG}" "${OTHER_IMAGE_TAGS}" "${IMAGE_SHA}" "${IMAGE_Z_TAG}" "${TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER}" "${BASE_IMAGE}"
}

add_acc_provision_artifacts() {
    cd /tmp/"${GIT_LOCAL_DIR}" || exit
    git pull --rebase origin ${GIT_BRANCH}
    mkdir -p /tmp/"${GIT_LOCAL_DIR}"/docs/release_artifacts/"${RELEASE_TAG}"/"${TRAVIS_REPO_SLUG##*/}" 2> /dev/null
    curl "https://api.travis-ci.com/v3/job/${TRAVIS_JOB_ID}/log.txt" > /tmp/"${GIT_LOCAL_DIR}"/docs/release_artifacts/"${RELEASE_TAG}"/"${TRAVIS_REPO_SLUG##*/}"/"${RELEASE_TAG}"-buildlog.txt
}

update_acc_provision_release(){
    python $SCRIPTS_DIR/update-release.py "${PYPI_REGISTRY}" "${TAG_NAME}"
}

git_add_commit_push() {
    cd /tmp/"${GIT_LOCAL_DIR}" || exit
    git config --local user.email "${GIT_EMAIL}"
    git config --local user.name "${GIT_USER}"
#     git stash save --include-untracked
#     git pull --rebase origin ${GIT_BRANCH}
#     git stash apply
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
    # Continuously check for the lock and acquire it when available
    while true; do
      if ! is_locked; then
        acquire_lock

        add_artifacts
        update_container_release
        add_trivy_vulnerabilites
        git_add_commit_push

        # Release the lock when changes are complete
        release_lock

        echo "Lock acquired and released."
        break
      else
        echo "Waiting to acquire the lock..."
      fi
      sleep $WAIT_INTERVAL
    done

else
    # Continuously check for the lock and acquire it when available
    while true; do
      if ! is_locked; then
        acquire_lock

        add_acc_provision_artifacts
        update_acc_provision_release
        git_add_commit_push

        # Release the lock when changes are complete
        release_lock

        echo "Lock acquired and released."
        break
      else
        echo "Waiting to acquire the lock..."
      fi
      sleep $WAIT_INTERVAL
    done

fi


