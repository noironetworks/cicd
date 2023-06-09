#!/bin/bash
set -x
SCRIPTS_DIR=$(dirname ${BASH_SOURCE[0]})
source "$SCRIPTS_DIR/globals.sh"

git show --summary

IMAGE_BUILD_REGISTRY="${QUAY_REGISTRY}"
IMAGE_BUILD_TAG=${IMAGE_TAG}
OTHER_IMAGE_TAGS="${TRAVIS_TAG_WITH_UPSTREAM_ID},${TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER}"
RELEASE_TAG_WITH_UPSTREAM_ID=${RELEASE_TAG}.${UPSTREAM_ID}

docker/copy_iptables.sh ${IMAGE_BUILD_REGISTRY}/opflex-build-base:${RELEASE_TAG_WITH_UPSTREAM_ID} dist-static

make -C . all-static

docker/travis/build-openvswitch-travis.sh ${IMAGE_BUILD_REGISTRY} ${IMAGE_TAG}
docker images
docker build -t ${IMAGE_BUILD_REGISTRY}/cnideploy:${IMAGE_TAG} --file=docker/travis/Dockerfile-cnideploy .
docker images
docker build -t ${IMAGE_BUILD_REGISTRY}/aci-containers-controller:${IMAGE_TAG} --file=docker/travis/Dockerfile-controller .
docker images
docker build -t ${IMAGE_BUILD_REGISTRY}/aci-containers-host:${IMAGE_TAG} --file=docker/travis/Dockerfile-host .
docker images
docker build -t ${IMAGE_BUILD_REGISTRY}/aci-containers-operator:${IMAGE_TAG} --file=docker/travis/Dockerfile-operator .
docker images

# Note: acc-provision-operator and opflex images come from their respective repos
ALL_IMAGES="aci-containers-host aci-containers-controller cnideploy aci-containers-operator openvswitch"
for IMAGE in ${ALL_IMAGES}; do
  $SCRIPTS_DIR/push-images.sh ${IMAGE_BUILD_REGISTRY} ${IMAGE} ${IMAGE_BUILD_TAG} ${OTHER_IMAGE_TAGS}
done
