#!/bin/bash
set -Eeuo pipefail
if [[ "${GITHUB_ACTIONS:-false}" != "true" ]]; then
  set -x
fi
SCRIPTS_DIR=$(dirname "${BASH_SOURCE[0]}")
source "$SCRIPTS_DIR/globals.sh"

IMAGE_BUILD_REGISTRY=$1
IMAGE=$2
IMAGE_BUILD_TAG=$3
OTHER_IMAGE_TAGS=${4//,/ }
BASE_IMAGE=$5
BUILT_IMAGE=${IMAGE_BUILD_REGISTRY}/${IMAGE}:${IMAGE_BUILD_TAG}
OTHER_IMAGE_TAGS="${OTHER_IMAGE_TAGS} ${IMAGE_Z_TAG}"

if [[ ! -x /tmp/grype ]]; then
  curl -sSfL https://raw.githubusercontent.com/anchore/grype/main/install.sh | sh -s -- -b /tmp
fi
if ! docker sbom --help >/dev/null 2>&1; then
  curl -sSfL https://raw.githubusercontent.com/docker/sbom-cli-plugin/main/install.sh | sh -s --
fi

emit_spdx_json() {
  local image_ref=$1
  if docker sbom --format spdx-json "${image_ref}" >/dev/null 2>&1; then
    docker sbom --format spdx-json "${image_ref}"
  elif docker sbom --output spdx-json "${image_ref}" >/dev/null 2>&1; then
    docker sbom --output spdx-json "${image_ref}"
  elif docker sbom -o spdx-json "${image_ref}" >/dev/null 2>&1; then
    docker sbom -o spdx-json "${image_ref}"
  else
    if [[ ! -x /tmp/syft ]]; then
      curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s -- -b /tmp
    fi
    /tmp/syft "${image_ref}" -o spdx-json
  fi
}

emit_text_sbom() {
  local image_ref=$1
  if docker sbom "${image_ref}" >/dev/null 2>&1; then
    docker sbom "${image_ref}"
  else
    if [[ ! -x /tmp/syft ]]; then
      curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s -- -b /tmp
    fi
    /tmp/syft "${image_ref}"
  fi
}

emit_spdx_json "${BUILT_IMAGE}" | /tmp/grype | tee /tmp/cve.txt
emit_text_sbom "${BUILT_IMAGE}" | tee /tmp/sbom.txt
emit_spdx_json "${BASE_IMAGE}" | /tmp/grype | tee /tmp/cve-base.txt

# Keep the two Quay accounts separate so subsequent builds can still pull noirolabs.
if [[ "${GITHUB_ACTIONS:-false}" == "true" ]]; then
  : "${QUAY_NOIRO_DOCKER_CONFIG:?QUAY_NOIRO_DOCKER_CONFIG is required}"
else
  docker login -u="$QUAY_TRAVIS_NOIROLABS_ROBO_USER" -p="$QUAY_TRAVIS_NOIROLABS_ROBO_PSWD" quay.io
fi
docker push "${BUILT_IMAGE}"
for OTHER_TAG in ${OTHER_IMAGE_TAGS}; do
  docker tag "${BUILT_IMAGE}" "${QUAY_REGISTRY}/${IMAGE}:${OTHER_TAG}"
  docker push "${QUAY_REGISTRY}/${IMAGE}:${OTHER_TAG}"
done

if [[ "${GITHUB_ACTIONS:-false}" != "true" ]]; then
  docker login -u="$QUAY_TRAVIS_NOIRO_ROBO_USER" -p="$QUAY_TRAVIS_NOIRO_ROBO_PSWD" quay.io
fi
for OTHER_TAG in "${TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER}" "${IMAGE_Z_TAG}"; do
  docker tag "${BUILT_IMAGE}" "${QUAY_NOIRO_REGISTRY}/${IMAGE}:${OTHER_TAG}"
  DOCKER_CONFIG="${QUAY_NOIRO_DOCKER_CONFIG:-${DOCKER_CONFIG:-$HOME/.docker}}" \
    docker push "${QUAY_NOIRO_REGISTRY}/${IMAGE}:${OTHER_TAG}"
done

if [[ "${GITHUB_ACTIONS:-false}" != "true" ]]; then
  docker login -u="$DOCKER_NOIRO_USER" -p="$DOCKER_NOIRO_PSWD" docker.io
fi
for OTHER_TAG in "${TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER}" "${IMAGE_Z_TAG}"; do
  docker tag "${BUILT_IMAGE}" "${DOCKER_REGISTRY}/${IMAGE}:${OTHER_TAG}"
  docker push "${DOCKER_REGISTRY}/${IMAGE}:${OTHER_TAG}"
done
