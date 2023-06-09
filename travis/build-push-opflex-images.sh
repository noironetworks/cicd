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

ALL_IMAGES="opflex-build-base opflex-build opflex"
for IMAGE in ${ALL_IMAGES}; do
  $SCRIPTS_DIR/push-images.sh ${IMAGE_BUILD_REGISTRY} ${IMAGE} ${IMAGE_BUILD_TAG} ${OTHER_IMAGE_TAGS}
done

# Revisit: We do the following so that we have an updated opflex-build-base image available in
# quay for aci-containers-host container builds
docker tag ${IMAGE_BUILD_REGISTRY}/opflex-build-base:${IMAGE_BUILD_TAG} ${IMAGE_BUILD_REGISTRY}/opflex-build-base:${RELEASE_TAG_WITH_UPSTREAM_ID}
docker push ${IMAGE_BUILD_REGISTRY}/opflex-build-base:${RELEASE_TAG_WITH_UPSTREAM_ID}
