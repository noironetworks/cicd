#!/bin/bash
set -x
SCRIPTS_DIR=$(dirname ${BASH_SOURCE[0]})
source "$SCRIPTS_DIR/globals.sh"

git show --summary

IMAGE_BUILD_REGISTRY="${QUAY_REGISTRY}"
IMAGE="acc-provision-operator"
IMAGE_BUILD_TAG=${IMAGE_TAG}
OTHER_IMAGE_TAGS="${TRAVIS_TAG_WITH_UPSTREAM_ID},${TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER}"

docker build -t ${IMAGE_BUILD_REGISTRY}/${IMAGE}:${IMAGE_BUILD_TAG} --file=Dockerfile .
docker images

#Fetching Base Image
BASE_IMAGE=$(grep -E '^FROM' Dockerfile | awk '{print $2}')
docker pull ${BASE_IMAGE}
docker images

$SCRIPTS_DIR/push-images.sh ${IMAGE_BUILD_REGISTRY} ${IMAGE} ${IMAGE_BUILD_TAG} ${OTHER_IMAGE_TAGS} ${BASE_IMAGE}
IMAGE_SHA=$(docker image inspect --format='{{.Id}}' "${IMAGE_BUILD_REGISTRY}/${IMAGE}:${IMAGE_BUILD_TAG}")
$SCRIPTS_DIR/push-to-cicd-status.sh ${QUAY_NOIRO_REGISTRY} ${IMAGE} ${IMAGE_BUILD_TAG} ${OTHER_IMAGE_TAGS} ${IMAGE_SHA} ${BASE_IMAGE}
