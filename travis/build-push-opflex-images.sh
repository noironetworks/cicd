#!/bin/bash
set -x
SCRIPTS_DIR=$(dirname ${BASH_SOURCE[0]})
source "$SCRIPTS_DIR/globals.sh"

git show --summary

IMAGE_BUILD_REGISTRY="${QUAY_REGISTRY}"
IMAGE_BUILD_TAG=${IMAGE_TAG}
OTHER_IMAGE_TAGS="${TRAVIS_TAG_WITH_UPSTREAM_ID},${TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER}"
RELEASE_TAG_WITH_UPSTREAM_ID=${RELEASE_TAG}.${UPSTREAM_ID}

docker/travis/build-opflex-travis.sh ${IMAGE_BUILD_REGISTRY} ${IMAGE_BUILD_TAG}
docker images

#Fetching Base Image - Common base image for every container so fetching once
BASE_IMAGE=$(grep -E '^FROM' docker/travis/Dockerfile-opflex | awk '{print $2}')
docker pull ${BASE_IMAGE}
docker images

# Check if the tag contains "opflex-build-base"
if [[ "${TRAVIS_TAG}" == *"opflex-build-base"* ]]; then
  BUILD_BASE=true
else
  BUILD_BASE=false
fi

if [[ "${BUILD_BASE}" == true ]]; then
  ALL_IMAGES="opflex-build-base"
  for IMAGE in ${ALL_IMAGES}; do
    $SCRIPTS_DIR/push-images.sh ${IMAGE_BUILD_REGISTRY} ${IMAGE} ${IMAGE_BUILD_TAG} ${OTHER_IMAGE_TAGS} ${BASE_IMAGE}
  done
else
  ALL_IMAGES="opflex-build opflex"
  for IMAGE in ${ALL_IMAGES}; do
    $SCRIPTS_DIR/push-images.sh ${IMAGE_BUILD_REGISTRY} ${IMAGE} ${IMAGE_BUILD_TAG} ${OTHER_IMAGE_TAGS} ${BASE_IMAGE}
  done

  IMAGE_SHA=$(docker image inspect --format='{{.Id}}' "${IMAGE_BUILD_REGISTRY}/opflex:${IMAGE_BUILD_TAG}")
  $SCRIPTS_DIR/push-to-cicd-status.sh ${QUAY_NOIRO_REGISTRY} opflex ${IMAGE_BUILD_TAG} ${OTHER_IMAGE_TAGS} ${IMAGE_SHA} ${BASE_IMAGE}
fi
