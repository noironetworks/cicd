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

# Capture each actual Dockerfile base before any child build. In particular,
# host and host-ovscni use ubi-minimal, while controller uses ubi. Pull each
# resolved base once, then capture the same local input for every child target.
if [[ "${GITHUB_ACTIONS:-false}" == "true" && "${CI_EVIDENCE_MODE:-}" == "prepared-v1" ]]; then
  BASE_IMAGES=(aci-containers-host aci-containers-host-ovscni aci-containers-controller cnideploy aci-containers-operator openvswitch aci-containers-webhook aci-containers-certmanager)
  BASE_FILES=(host host controller cnideploy operator openvswitch webhook certmanager)
  for ((base_index=0; base_index<${#BASE_IMAGES[@]}; base_index++)); do
    python3 "$SCRIPTS_DIR/scan_result.py" capture-base \
      --dockerfile "docker/travis/Dockerfile-${BASE_FILES[$base_index]}" \
      --pull-once-file "${CI_ARTIFACT_DIR}/base-provenance/.pulled.json" \
      --output "${CI_ARTIFACT_DIR}/base-provenance/${BASE_IMAGES[$base_index]}.json"
  done
fi

IPTABLES_BASE_REF="${IMAGE_BUILD_REGISTRY}/opflex-build-base:${UPSTREAM_IMAGE_Z_TAG}"
if [[ "${GITHUB_ACTIONS:-false}" == "true" && "${CI_EVIDENCE_MODE:-}" == "prepared-v1" ]]; then
  : "${EXPECTED_OPFLEX_BASE_DIGEST:?EXPECTED_OPFLEX_BASE_DIGEST is required for the approved iptables input}"
  [[ "${EXPECTED_OPFLEX_BASE_DIGEST}" =~ ^sha256:[0-9a-f]{64}$ ]] || {
    echo "Invalid approved iptables build-base digest" >&2
    exit 1
  }
  IPTABLES_BASE_REF="${IMAGE_BUILD_REGISTRY}/opflex-build-base@${EXPECTED_OPFLEX_BASE_DIGEST}"
  python3 "$SCRIPTS_DIR/scan_result.py" capture-input \
    --reference "${IPTABLES_BASE_REF}" --expected-digest "${EXPECTED_OPFLEX_BASE_DIGEST}" \
    --output "${CI_ARTIFACT_DIR}/base-provenance/aci-iptables-base.json"
fi
docker/copy_iptables.sh "${IPTABLES_BASE_REF}" dist-static
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

# For GitHub Actions: prepare metadata file for separate status publication step
# For Travis: status calls remain inline in loop (backward compatibility)
if [[ "${GITHUB_ACTIONS:-false}" == "true" ]]; then
  IMAGES_METADATA_FILE="${CI_ARTIFACT_DIR}/aci-images-metadata.jsonl"
  rm -f "$IMAGES_METADATA_FILE"
fi

for IMAGE in "${ALL_IMAGES[@]}"; do
  if [[ "${IMAGE}" != "openvswitch" ]]; then
    $SCRIPTS_DIR/push-images.sh "${IMAGE_BUILD_REGISTRY}" "${IMAGE}" "${IMAGE_BUILD_TAG}" "${OTHER_IMAGE_TAGS}" "${ACI_BASE_IMAGE}"
    IMAGE_SHA=$(docker image inspect --format='{{.Id}}' "${IMAGE_BUILD_REGISTRY}/${IMAGE}:${IMAGE_BUILD_TAG}")
    if [[ "${GITHUB_ACTIONS:-false}" == "true" ]]; then
      printf '%s\n' "{\"image\":\"${IMAGE}\",\"registry\":\"${QUAY_NOIRO_REGISTRY}\",\"tag\":\"${IMAGE_BUILD_TAG}\",\"other_tags\":\"${OTHER_IMAGE_TAGS}\",\"sha\":\"${IMAGE_SHA}\",\"base_image\":\"${ACI_BASE_IMAGE}\"}" >> "$IMAGES_METADATA_FILE"
    else
      if [[ "${SKIP_CICD_STATUS:-false}" != "true" ]]; then
        $SCRIPTS_DIR/push-to-cicd-status.sh "${QUAY_NOIRO_REGISTRY}" "${IMAGE}" "${IMAGE_BUILD_TAG}" "${OTHER_IMAGE_TAGS}" "${IMAGE_SHA}" "${ACI_BASE_IMAGE}" || {
          echo "WARNING: Failed to publish status for ${IMAGE}, but image push succeeded - continuing"
        }
      fi
    fi
  else
    $SCRIPTS_DIR/push-images.sh "${IMAGE_BUILD_REGISTRY}" "${IMAGE}" "${IMAGE_BUILD_TAG}" "${OTHER_IMAGE_TAGS}" "${OVS_BASE_IMAGE}"
    IMAGE_SHA=$(docker image inspect --format='{{.Id}}' "${IMAGE_BUILD_REGISTRY}/${IMAGE}:${IMAGE_BUILD_TAG}")
    if [[ "${GITHUB_ACTIONS:-false}" == "true" ]]; then
      printf '%s\n' "{\"image\":\"${IMAGE}\",\"registry\":\"${QUAY_NOIRO_REGISTRY}\",\"tag\":\"${IMAGE_BUILD_TAG}\",\"other_tags\":\"${OTHER_IMAGE_TAGS}\",\"sha\":\"${IMAGE_SHA}\",\"base_image\":\"${OVS_BASE_IMAGE}\"}" >> "$IMAGES_METADATA_FILE"
    else
      if [[ "${SKIP_CICD_STATUS:-false}" != "true" ]]; then
        $SCRIPTS_DIR/push-to-cicd-status.sh "${QUAY_NOIRO_REGISTRY}" "${IMAGE}" "${IMAGE_BUILD_TAG}" "${OTHER_IMAGE_TAGS}" "${IMAGE_SHA}" "${OVS_BASE_IMAGE}" || {
          echo "WARNING: Failed to publish status for ${IMAGE}, but image push succeeded - continuing"
        }
      fi
    fi
  fi
done
