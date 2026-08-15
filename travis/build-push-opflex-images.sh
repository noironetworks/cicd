#!/bin/bash
if [[ "${GITHUB_ACTIONS:-false}" == "true" ]]; then
  set -Eeuo pipefail
else
  set -x
fi
SCRIPTS_DIR=$(dirname ${BASH_SOURCE[0]})
source "$SCRIPTS_DIR/globals.sh"

git show --summary

IMAGE_BUILD_REGISTRY="${QUAY_REGISTRY}"
IMAGE_BUILD_TAG=${IMAGE_TAG}
OTHER_IMAGE_TAGS="${TRAVIS_TAG_WITH_UPSTREAM_ID},${TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER}"
RELEASE_TAG_WITH_UPSTREAM_ID=${RELEASE_TAG}.${UPSTREAM_ID}

if [[ "${GITHUB_ACTIONS:-false}" == "true" ]]; then
  : "${GHA_TRIGGER_TAG:?GHA_TRIGGER_TAG is required for a GitHub Actions build}"
  : "${GHA_DATE_TAG:?GHA_DATE_TAG is required for a GitHub Actions build}"
  : "${GHA_OPFLEX_BUILD_PHASE:?GHA_OPFLEX_BUILD_PHASE is required for a GitHub Actions build}"
  : "${TRAVIS_BUILD_NUMBER:?TRAVIS_BUILD_NUMBER is required for a GitHub Actions build}"
  : "${CI_ARTIFACT_DIR:?CI_ARTIFACT_DIR is required for a GitHub Actions build}"

  # Validate the release form (prefix match) like Travis check-git-tag.sh, not an exact tag.
  [[ "${GHA_TRIGGER_TAG}" == "${RELEASE_TAG}"* ]] || {
    echo "GitHub Actions trigger tag ${GHA_TRIGGER_TAG} must match release ${RELEASE_TAG}" >&2
    exit 1
  }
  [[ "${GITHUB_REPOSITORY:-}" == "noironetworks/opflex" ]] || {
    echo "GitHub Actions build is restricted to noironetworks/opflex" >&2
    exit 1
  }
  [[ "${GITHUB_REF:-}" == "refs/tags/${GHA_TRIGGER_TAG}" ]] || {
    echo "GitHub Actions build is restricted to tag ${GHA_TRIGGER_TAG}" >&2
    exit 1
  }
  [[ "${TRAVIS_TAG:-}" == "${RELEASE_TAG}" ]] || {
    echo "GitHub Actions must use release tag ${RELEASE_TAG} for image naming" >&2
    exit 1
  }
  [[ "${GHA_DATE_TAG}" =~ ^[0-9]{6}$ ]] || {
    echo "GHA_DATE_TAG must use MMDDYY format" >&2
    exit 1
  }
  [[ "${TRAVIS_BUILD_NUMBER}" =~ ^[1-9][0-9]*$ ]] || {
    echo "TRAVIS_BUILD_NUMBER must be a positive integer" >&2
    exit 1
  }

  case "${GHA_OPFLEX_BUILD_PHASE}" in
    base)
      BUILD_BASE=true
      ;;
    runtime)
      BUILD_BASE=false
      ;;
    *)
      echo "GHA_OPFLEX_BUILD_PHASE must be base or runtime" >&2
      exit 1
      ;;
  esac
  IMAGE_BUILD_TAG="${RELEASE_TAG_WITH_UPSTREAM_ID}"
  OTHER_IMAGE_TAGS="${RELEASE_TAG_WITH_UPSTREAM_ID},${RELEASE_TAG_WITH_UPSTREAM_ID}.${GHA_DATE_TAG}.${TRAVIS_BUILD_NUMBER}"
else
  # Travis selects the build-base job from the release tag name.
  if [[ "${TRAVIS_TAG}" == *"opflex-build-base"* ]]; then
    BUILD_BASE=true
    IMAGE_BUILD_TAG=${RELEASE_TAG_WITH_UPSTREAM_ID}
    OTHER_IMAGE_TAGS="${RELEASE_TAG_WITH_UPSTREAM_ID},${RELEASE_TAG_WITH_UPSTREAM_ID}.${DATE_TAG}.${TRAVIS_BUILD_NUMBER},${UPSTREAM_IMAGE_Z_TAG}"
  else
    BUILD_BASE=false
  fi
fi

if [[ "${GITHUB_ACTIONS:-false}" == "true" ]]; then
  mkdir -p "${CI_ARTIFACT_DIR}"
  printf 'image\tdigest\n' > "${CI_ARTIFACT_DIR}/quay-published-images.tsv"
  printf 'image\tdigest\n' > "${CI_ARTIFACT_DIR}/quay-noiro-published-images.tsv"
  printf 'image\tdigest\n' > "${CI_ARTIFACT_DIR}/docker-published-images.tsv"
fi

docker/travis/build-opflex-travis.sh "${IMAGE_BUILD_REGISTRY}" "${IMAGE_BUILD_TAG}"
docker images

#Fetching Base Image - Common base image for every container so fetching once
BASE_IMAGE=$(grep -E '^FROM' docker/travis/Dockerfile-opflex | awk '{print $2}')
if [[ "${GITHUB_ACTIONS:-false}" != "true" ]]; then
  docker pull "${BASE_IMAGE}"
fi
docker images


if [[ "${BUILD_BASE}" == true ]]; then
  ALL_IMAGES=("opflex-build-base")
  for IMAGE in "${ALL_IMAGES[@]}"; do
    "$SCRIPTS_DIR/push-images.sh" "${IMAGE_BUILD_REGISTRY}" "${IMAGE}" "${IMAGE_BUILD_TAG}" "${OTHER_IMAGE_TAGS}" "${BASE_IMAGE}"
  done
else
  if [[ "${GITHUB_ACTIONS:-false}" == "true" ]]; then
    # opflex-build is an intermediate image used only while assembling the
    # runtime image. The published OpFlex artifacts are build-base and opflex.
    ALL_IMAGES=("opflex")
  else
    ALL_IMAGES=("opflex-build" "opflex")
  fi
  for IMAGE in "${ALL_IMAGES[@]}"; do
    "$SCRIPTS_DIR/push-images.sh" "${IMAGE_BUILD_REGISTRY}" "${IMAGE}" "${IMAGE_BUILD_TAG}" "${OTHER_IMAGE_TAGS}" "${BASE_IMAGE}"
  done

  if [[ "${GITHUB_ACTIONS:-false}" != "true" && "${SKIP_CICD_STATUS:-false}" != "true" ]]; then
    IMAGE_SHA=$(docker image inspect --format='{{.Id}}' "${IMAGE_BUILD_REGISTRY}/opflex:${IMAGE_BUILD_TAG}")
    "$SCRIPTS_DIR/push-to-cicd-status.sh" "${QUAY_NOIRO_REGISTRY}" opflex "${IMAGE_BUILD_TAG}" "${OTHER_IMAGE_TAGS}" "${IMAGE_SHA}" "${BASE_IMAGE}"
  fi
fi
