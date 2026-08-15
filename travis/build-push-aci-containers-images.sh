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
  : "${TRAVIS_BUILD_NUMBER:?TRAVIS_BUILD_NUMBER is required for a GitHub Actions build}"
  : "${CI_ARTIFACT_DIR:?CI_ARTIFACT_DIR is required for a GitHub Actions build}"
  # Validate the release form (prefix match) like Travis check-git-tag.sh, not an exact tag.
  [[ "${GHA_TRIGGER_TAG}" == "${RELEASE_TAG}"* ]] || {
    echo "GitHub Actions trigger tag ${GHA_TRIGGER_TAG} must match release ${RELEASE_TAG}" >&2
    exit 1
  }
  [[ "${GITHUB_REPOSITORY:-}" == "noironetworks/aci-containers" ]] || {
    echo "GitHub Actions build is restricted to noironetworks/aci-containers" >&2
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
  IMAGE_TAG="${RELEASE_TAG_WITH_UPSTREAM_ID}"
  IMAGE_BUILD_TAG="${RELEASE_TAG_WITH_UPSTREAM_ID}"
  OTHER_IMAGE_TAGS="${RELEASE_TAG_WITH_UPSTREAM_ID},${RELEASE_TAG_WITH_UPSTREAM_ID}.${GHA_DATE_TAG}.${TRAVIS_BUILD_NUMBER}"
fi

if [[ "${GITHUB_ACTIONS:-false}" == "true" ]]; then
  mkdir -p "${CI_ARTIFACT_DIR}"
  printf 'image\tdigest\n' > "${CI_ARTIFACT_DIR}/quay-published-images.tsv"
  printf 'image\tdigest\n' > "${CI_ARTIFACT_DIR}/quay-noiro-published-images.tsv"
  printf 'image\tdigest\n' > "${CI_ARTIFACT_DIR}/docker-published-images.tsv"
fi

rm -f \
  dist-static/iptables-bin.tar.gz \
  dist-static/iptables-libs.tar.gz \
  dist-static/iptables-wrapper-installer.sh
docker/copy_iptables.sh ${IMAGE_BUILD_REGISTRY}/opflex-build-base:${UPSTREAM_IMAGE_Z_TAG} dist-static
tar -tzf dist-static/iptables-bin.tar.gz >/dev/null
tar -tzf dist-static/iptables-libs.tar.gz >/dev/null
[[ -s dist-static/iptables-wrapper-installer.sh ]]

if [[ "${GITHUB_ACTIONS:-false}" == "true" ]]; then
  docker container prune --force >/dev/null
  docker image rm "${IMAGE_BUILD_REGISTRY}/opflex-build-base:${UPSTREAM_IMAGE_Z_TAG}" >/dev/null || true
fi

make -C . all-static

docker/travis/build-openvswitch-travis.sh ${IMAGE_BUILD_REGISTRY} ${IMAGE_TAG}
if [[ "${GITHUB_ACTIONS:-false}" == "true" ]]; then
  docker image rm "${IMAGE_BUILD_REGISTRY}/openvswitch-base:${IMAGE_TAG}" >/dev/null || true
  docker builder prune --force >/dev/null
fi
docker images
docker build -t ${IMAGE_BUILD_REGISTRY}/cnideploy:${IMAGE_TAG} --file=docker/travis/Dockerfile-cnideploy .
docker images
docker build -t ${IMAGE_BUILD_REGISTRY}/aci-containers-controller:${IMAGE_TAG} --file=docker/travis/Dockerfile-controller .
docker images
docker build --target without-ovscni -t ${IMAGE_BUILD_REGISTRY}/aci-containers-host:${IMAGE_TAG} --file=docker/travis/Dockerfile-host .
docker images
docker build --target with-ovscni -t ${IMAGE_BUILD_REGISTRY}/aci-containers-host-ovscni:${IMAGE_TAG} --file=docker/travis/Dockerfile-host .
docker images
docker build -t ${IMAGE_BUILD_REGISTRY}/aci-containers-operator:${IMAGE_TAG} --file=docker/travis/Dockerfile-operator .
docker images
docker build -t ${IMAGE_BUILD_REGISTRY}/aci-containers-webhook:${IMAGE_TAG} --file=docker/travis/Dockerfile-webhook .
docker images
docker build -t ${IMAGE_BUILD_REGISTRY}/aci-containers-certmanager:${IMAGE_TAG} --file=docker/travis/Dockerfile-certmanager .
docker images

# Fetching Base Image - Common base image for every ACI container so fetching once
ACI_BASE_IMAGE=$(grep -E '^FROM' docker/travis/Dockerfile-controller | awk '{print $2}')
docker pull "${ACI_BASE_IMAGE}"
docker images

# Fetching Base Image for openvswitch
OVS_BASE_IMAGE=$(grep -E '^FROM' docker/travis/Dockerfile-openvswitch | awk '{print $2}')
docker pull "${OVS_BASE_IMAGE}"
docker images

# Note: opflex images come from their respective repos
ALL_IMAGES=("aci-containers-host" "aci-containers-controller" "cnideploy" "aci-containers-operator" "openvswitch" "aci-containers-webhook" "aci-containers-certmanager" "aci-containers-host-ovscni")
for IMAGE in "${ALL_IMAGES[@]}"; do
  if [[ "${IMAGE}" != "openvswitch" ]]; then
    $SCRIPTS_DIR/push-images.sh "${IMAGE_BUILD_REGISTRY}" "${IMAGE}" "${IMAGE_BUILD_TAG}" "${OTHER_IMAGE_TAGS}" "${ACI_BASE_IMAGE}"
    if [[ "${SKIP_CICD_STATUS:-false}" != "true" ]]; then
      IMAGE_SHA=$(docker image inspect --format='{{.Id}}' "${IMAGE_BUILD_REGISTRY}/${IMAGE}:${IMAGE_BUILD_TAG}")
      $SCRIPTS_DIR/push-to-cicd-status.sh "${QUAY_NOIRO_REGISTRY}" "${IMAGE}" "${IMAGE_BUILD_TAG}" "${OTHER_IMAGE_TAGS}" "${IMAGE_SHA}" "${ACI_BASE_IMAGE}"
    fi
  else
    $SCRIPTS_DIR/push-images.sh "${IMAGE_BUILD_REGISTRY}" "${IMAGE}" "${IMAGE_BUILD_TAG}" "${OTHER_IMAGE_TAGS}" "${OVS_BASE_IMAGE}"
    if [[ "${SKIP_CICD_STATUS:-false}" != "true" ]]; then
      IMAGE_SHA=$(docker image inspect --format='{{.Id}}' "${IMAGE_BUILD_REGISTRY}/${IMAGE}:${IMAGE_BUILD_TAG}")
      $SCRIPTS_DIR/push-to-cicd-status.sh "${QUAY_NOIRO_REGISTRY}" "${IMAGE}" "${IMAGE_BUILD_TAG}" "${OTHER_IMAGE_TAGS}" "${IMAGE_SHA}" "${OVS_BASE_IMAGE}"
    fi
  fi
done
