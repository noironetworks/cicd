#!/bin/bash
set -x
SCRIPTS_DIR=$(dirname ${BASH_SOURCE[0]})
source "$SCRIPTS_DIR/globals.sh"

git show --summary

IMAGE_BUILD_REGISTRY="${QUAY_REGISTRY}"
IMAGE_BUILD_TAG=${IMAGE_TAG}
OTHER_IMAGE_TAGS="${TRAVIS_TAG_WITH_UPSTREAM_ID},${TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER}"
RELEASE_TAG_WITH_UPSTREAM_ID=${RELEASE_TAG}.${UPSTREAM_ID}


wget -P "$WORKSPACE" https://musl.cc/aarch64-linux-musl-cross.tgz
tar -xvf "$WORKSPACE/aarch64-linux-musl-cross.tgz" -C "$WORKSPACE"
cp "$WORKSPACE/aarch64-linux-musl-cross/bin/aarch64-linux-musl-gcc" "$WORKSPACE"

docker/copy_iptables.sh ${IMAGE_BUILD_REGISTRY}/opflex-build-base:${UPSTREAM_IMAGE_Z_TAG} dist-static

export CC="$WORKSPACE/aarch64-linux-musl-gcc"
export GOARCH=arm64

make -C . all-static-arm6

docker buildx build --no-cache -t <tag>  --platform linux/arm64 -f Dockerfile --push .


$SCRIPTS_DIR/build-openvswitch-travis-arm64.sh ${IMAGE_BUILD_REGISTRY} ${IMAGE_TAG}
docker images
docker buildx build --no-cache --platform linux/arm64 -t ${IMAGE_BUILD_REGISTRY}/cnideploy:${IMAGE_TAG} --file=docker/travis/Dockerfile-cnideploy-arm64 .
docker images
docker buildx build --no-cache --platform linux/arm64 -t ${IMAGE_BUILD_REGISTRY}/aci-containers-controller:${IMAGE_TAG} --file=docker/travis/Dockerfile-controller-arm64 .
docker images
docker buildx build --no-cache --platform linux/arm64 --target without-ovscni -t ${IMAGE_BUILD_REGISTRY}/aci-containers-host:${IMAGE_TAG} --file=docker/travis/Dockerfile-host-arm64 .
docker images
docker buildx build --no-cache --platform linux/arm64 --target with-ovscni -t ${IMAGE_BUILD_REGISTRY}/aci-containers-host-ovscni:${IMAGE_TAG} --file=docker/travis/Dockerfile-host .
docker images
ddocker buildx build --no-cache --platform linux/arm64 -t ${IMAGE_BUILD_REGISTRY}/aci-containers-operator:${IMAGE_TAG} --file=docker/travis/Dockerfile-operator-arm64 .
docker images
docker buildx build --no-cache --platform linux/arm64 -t ${IMAGE_BUILD_REGISTRY}/aci-containers-webhook:${IMAGE_TAG} --file=docker/travis/Dockerfile-webhook .
docker images
docker buildx build --no-cache --platform linux/arm64 -t ${IMAGE_BUILD_REGISTRY}/aci-containers-certmanager:${IMAGE_TAG} --file=docker/travis/Dockerfile-certmanager .
docker images

# Fetching Base Image - Common base image for every ACI container so fetching once
ACI_BASE_IMAGE=$(grep -E '^FROM' docker/travis/Dockerfile-controller | awk '{print $2}')
docker pull "${ACI_BASE_IMAGE}"
docker images

# Fetching Base Image for openvswitch
OVS_BASE_IMAGE=$(grep -E '^FROM' docker/travis/Dockerfile-openvswitch | awk '{print $2}')
docker pull "${OVS_BASE_IMAGE}"
docker images


'
# Note: acc-provision-operator and opflex images come from their respective repos
ALL_IMAGES=("aci-containers-host" "aci-containers-controller" "cnideploy" "aci-containers-operator" "openvswitch" "aci-containers-webhook" "aci-containers-certmanager" "aci-containers-host-ovscni")
for IMAGE in "${ALL_IMAGES[@]}"; do
  if [[ "${IMAGE}" != "openvswitch" ]]; then
    $SCRIPTS_DIR/push-images.sh "${IMAGE_BUILD_REGISTRY}" "${IMAGE}" "${IMAGE_BUILD_TAG}" "${OTHER_IMAGE_TAGS}" "${ACI_BASE_IMAGE}"
    IMAGE_SHA=$(docker image inspect --format='{{.Id}}' "${IMAGE_BUILD_REGISTRY}/${IMAGE}:${IMAGE_BUILD_TAG}")
    $SCRIPTS_DIR/push-to-cicd-status.sh "${QUAY_NOIRO_REGISTRY}" "${IMAGE}" "${IMAGE_BUILD_TAG}" "${OTHER_IMAGE_TAGS}" "${IMAGE_SHA}" "${ACI_BASE_IMAGE}"
  else
    $SCRIPTS_DIR/push-images.sh "${IMAGE_BUILD_REGISTRY}" "${IMAGE}" "${IMAGE_BUILD_TAG}" "${OTHER_IMAGE_TAGS}" "${OVS_BASE_IMAGE}"
    IMAGE_SHA=$(docker image inspect --format='{{.Id}}' "${IMAGE_BUILD_REGISTRY}/${IMAGE}:${IMAGE_BUILD_TAG}")
    $SCRIPTS_DIR/push-to-cicd-status.sh "${QUAY_NOIRO_REGISTRY}" "${IMAGE}" "${IMAGE_BUILD_TAG}" "${OTHER_IMAGE_TAGS}" "${IMAGE_SHA}" "${OVS_BASE_IMAGE}"
  fi
done
'