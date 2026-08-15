#!/bin/bash
if [[ "${GITHUB_ACTIONS:-false}" == "true" ]]; then
  set -Eeuo pipefail
else
  set -x
fi
SCRIPTS_DIR=$(dirname ${BASH_SOURCE[0]})
source "$SCRIPTS_DIR/globals.sh"

IMAGE_BUILD_REGISTRY=$1
IMAGE=$2
IMAGE_BUILD_TAG=$3
OTHER_IMAGE_TAGS=${4//,/ }
BASE_IMAGE=$5

BUILT_IMAGE=${IMAGE_BUILD_REGISTRY}/${IMAGE}:${IMAGE_BUILD_TAG}

if [[ "${GITHUB_ACTIONS:-false}" == "true" ]]; then
  : "${GHA_TRIGGER_TAG:?GHA_TRIGGER_TAG is required}"
  : "${GHA_DATE_TAG:?GHA_DATE_TAG is required}"
  : "${TRAVIS_BUILD_NUMBER:?TRAVIS_BUILD_NUMBER is required}"
  : "${CI_ARTIFACT_DIR:?CI_ARTIFACT_DIR is required}"

  case "${GITHUB_REPOSITORY:-}" in
    noironetworks/aci-containers)
      case "${IMAGE}" in
        aci-containers-host | aci-containers-controller | cnideploy | aci-containers-operator | openvswitch | aci-containers-webhook | aci-containers-certmanager | aci-containers-host-ovscni)
          ;;
        *)
          echo "Refusing unexpected ACI containers image ${IMAGE}" >&2
          exit 1
          ;;
      esac
      ;;
    noironetworks/opflex)
      case "${IMAGE}" in
        opflex-build-base | opflex)
          ;;
        *)
          echo "Refusing unexpected published opflex image ${IMAGE}" >&2
          exit 1
          ;;
      esac
      ;;
    *)
      echo "GitHub Actions publication is not allowed from ${GITHUB_REPOSITORY:-unset}" >&2
      exit 1
      ;;
  esac

  # Validate the release form (prefix match) like Travis check-git-tag.sh, not an exact tag.
  [[ "${GHA_TRIGGER_TAG}" == "${RELEASE_TAG}"* ]] || {
    echo "GitHub Actions trigger tag ${GHA_TRIGGER_TAG} must match release ${RELEASE_TAG}" >&2
    exit 1
  }
  [[ "${GITHUB_REF:-}" == "refs/tags/${GHA_TRIGGER_TAG}" ]] || {
    echo "GitHub Actions publication is restricted to tag ${GHA_TRIGGER_TAG}" >&2
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

  PLAIN_TAG="${RELEASE_TAG}.${UPSTREAM_ID}"
  DATED_TAG="${PLAIN_TAG}.${GHA_DATE_TAG}.${TRAVIS_BUILD_NUMBER}"
  Z_TAG="${PLAIN_TAG}.z"
  [[ "${UPSTREAM_IMAGE_TAG}" == "${PLAIN_TAG}" ]] || {
    echo "Refusing unexpected upstream image tag ${UPSTREAM_IMAGE_TAG}" >&2
    exit 1
  }
  [[ "${UPSTREAM_IMAGE_Z_TAG}" == "${Z_TAG}" && "${IMAGE_Z_TAG}" == "${Z_TAG}" ]] || {
    echo "Refusing unexpected legacy z image tag" >&2
    exit 1
  }
  [[ "${GHA_DATED_IMAGE_TAG}" == "${DATED_TAG}" && "${TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER}" == "${DATED_TAG}" ]] || {
    echo "Refusing unexpected dated image tag" >&2
    exit 1
  }
  [[ "${IMAGE_BUILD_REGISTRY}" == "${QUAY_REGISTRY}" ]] || {
    echo "Refusing unexpected image build registry ${IMAGE_BUILD_REGISTRY}" >&2
    exit 1
  }
  [[ "${IMAGE_BUILD_TAG}" == "${PLAIN_TAG}" && "${BUILT_IMAGE}" == "${QUAY_REGISTRY}/${IMAGE}:${PLAIN_TAG}" ]] || {
    echo "Refusing unexpected built image ${BUILT_IMAGE}" >&2
    exit 1
  }
  [[ "${OTHER_IMAGE_TAGS}" == "${PLAIN_TAG} ${DATED_TAG}" ]] || {
    echo "Refusing unexpected additional image tags ${OTHER_IMAGE_TAGS}" >&2
    exit 1
  }

  : "${DOCKER_CONFIG:?DOCKER_CONFIG is required}"
  [[ -s "${DOCKER_CONFIG}/config.json" && ! -L "${DOCKER_CONFIG}/config.json" ]] || {
    echo "Missing temporary Docker registry configuration" >&2
    exit 1
  }
  : "${QUAY_NOIRO_DOCKER_CONFIG:?QUAY_NOIRO_DOCKER_CONFIG is required}"
  [[ -s "${QUAY_NOIRO_DOCKER_CONFIG}/config.json" && ! -L "${QUAY_NOIRO_DOCKER_CONFIG}/config.json" ]] || {
    echo "Missing temporary quay.io/noiro Docker registry configuration" >&2
    exit 1
  }
  DEFAULT_DOCKER_CONFIG_PATH=$(cd -- "${DOCKER_CONFIG}" && pwd -P)
  QUAY_NOIRO_DOCKER_CONFIG_PATH=$(cd -- "${QUAY_NOIRO_DOCKER_CONFIG}" && pwd -P)
  [[ "${DEFAULT_DOCKER_CONFIG_PATH}" != "${QUAY_NOIRO_DOCKER_CONFIG_PATH}" ]] || {
    echo "quay.io/noiro must use a Docker credential directory separate from DOCKER_CONFIG" >&2
    exit 1
  }

  QUAY_MANIFEST="${CI_ARTIFACT_DIR}/quay-published-images.tsv"
  QUAY_NOIRO_MANIFEST="${CI_ARTIFACT_DIR}/quay-noiro-published-images.tsv"
  DOCKER_MANIFEST="${CI_ARTIFACT_DIR}/docker-published-images.tsv"
  for MANIFEST in "${QUAY_MANIFEST}" "${QUAY_NOIRO_MANIFEST}" "${DOCKER_MANIFEST}"; do
    [[ -f "${MANIFEST}" && ! -L "${MANIFEST}" ]] || {
      echo "Missing publication manifest ${MANIFEST}" >&2
      exit 1
    }
    [[ "$(head -n 1 "${MANIFEST}")" == $'image\tdigest' ]] || {
      echo "Invalid publication manifest header in ${MANIFEST}" >&2
      exit 1
    }
  done

  PUSHED_DIGEST=""
  push_and_record() {
    local target_image=$1
    local manifest=$2
    local docker_config=$3
    local log_prefix=$4
    local push_log="${CI_ARTIFACT_DIR}/${log_prefix}-push-${IMAGE}-${target_image##*:}.log"

    if [[ "${target_image}" != "${BUILT_IMAGE}" ]]; then
      docker image tag "${BUILT_IMAGE}" "${target_image}"
    fi
    if [[ -n "${docker_config}" ]]; then
      DOCKER_CONFIG="${docker_config}" docker push "${target_image}" | tee "${push_log}"
    else
      docker push "${target_image}" | tee "${push_log}"
    fi
    PUSHED_DIGEST=$(sed -nE 's/^.*digest: (sha256:[0-9a-f]{64}).*$/\1/p' "${push_log}" | tail -n 1)
    [[ "${PUSHED_DIGEST}" =~ ^sha256:[0-9a-f]{64}$ ]] || {
      echo "Unable to capture pushed digest for ${target_image}" >&2
      exit 1
    }
    printf '%s\t%s\n' "${target_image}" "${PUSHED_DIGEST}" >> "${manifest}"
  }

  push_and_record "${QUAY_REGISTRY}/${IMAGE}:${PLAIN_TAG}" "${QUAY_MANIFEST}" "" quay
  NOIROLABS_DIGEST=${PUSHED_DIGEST}
  push_and_record "${QUAY_REGISTRY}/${IMAGE}:${DATED_TAG}" "${QUAY_MANIFEST}" "" quay
  [[ "${PUSHED_DIGEST}" == "${NOIROLABS_DIGEST}" ]] || {
    echo "Dated quay.io/noirolabs digest does not match the plain tag" >&2
    exit 1
  }

  push_and_record "${QUAY_NOIRO_REGISTRY}/${IMAGE}:${DATED_TAG}" "${QUAY_NOIRO_MANIFEST}" "${QUAY_NOIRO_DOCKER_CONFIG}" quay-noiro
  QUAY_NOIRO_DIGEST=${PUSHED_DIGEST}

  push_and_record "${DOCKER_REGISTRY}/${IMAGE}:${DATED_TAG}" "${DOCKER_MANIFEST}" "" docker
  DOCKER_DIGEST=${PUSHED_DIGEST}

  # Move the legacy z aliases only after every non-moving tag is available.
  # noirolabs is last because ACI and OpFlex builds consume its z aliases.
  push_and_record "${QUAY_NOIRO_REGISTRY}/${IMAGE}:${Z_TAG}" "${QUAY_NOIRO_MANIFEST}" "${QUAY_NOIRO_DOCKER_CONFIG}" quay-noiro
  [[ "${PUSHED_DIGEST}" == "${QUAY_NOIRO_DIGEST}" ]] || {
    echo "Legacy z quay.io/noiro digest does not match the dated tag" >&2
    exit 1
  }

  push_and_record "${DOCKER_REGISTRY}/${IMAGE}:${Z_TAG}" "${DOCKER_MANIFEST}" "" docker
  [[ "${PUSHED_DIGEST}" == "${DOCKER_DIGEST}" ]] || {
    echo "Legacy z Docker Hub digest does not match the dated tag" >&2
    exit 1
  }

  push_and_record "${QUAY_REGISTRY}/${IMAGE}:${Z_TAG}" "${QUAY_MANIFEST}" "" quay
  [[ "${PUSHED_DIGEST}" == "${NOIROLABS_DIGEST}" ]] || {
    echo "Legacy z quay.io/noirolabs digest does not match the plain tag" >&2
    exit 1
  }

  SECURITY_ARTIFACT_DIR="${CI_ARTIFACT_DIR}/security-artifacts/${IMAGE}"
  mkdir -p "${SECURITY_ARTIFACT_DIR}"

  if [[ ! -x "/tmp/grype" ]]; then
    curl -sSfL https://raw.githubusercontent.com/anchore/grype/main/install.sh | sh -s -- -b /tmp
  fi
  if ! docker sbom --help >/dev/null 2>&1; then
    curl -sSfL https://raw.githubusercontent.com/docker/sbom-cli-plugin/main/install.sh | sh -s --
  fi

  emit_spdx_json() {
    local image_ref=$1
    if docker sbom --format spdx-json "${image_ref}" >/dev/null 2>&1; then
      docker sbom --format spdx-json "${image_ref}"
      return 0
    fi
    if docker sbom --output spdx-json "${image_ref}" >/dev/null 2>&1; then
      docker sbom --output spdx-json "${image_ref}"
      return 0
    fi
    if docker sbom -o spdx-json "${image_ref}" >/dev/null 2>&1; then
      docker sbom -o spdx-json "${image_ref}"
      return 0
    fi

    if [[ ! -x "/tmp/syft" ]]; then
      curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s -- -b /tmp
    fi
    /tmp/syft "${image_ref}" -o spdx-json
  }

  emit_text_sbom() {
    local image_ref=$1
    if docker sbom "${image_ref}" >/dev/null 2>&1; then
      docker sbom "${image_ref}"
      return 0
    fi
    if [[ ! -x "/tmp/syft" ]]; then
      curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s -- -b /tmp
    fi
    /tmp/syft "${image_ref}"
  }

  emit_spdx_json "${BUILT_IMAGE}" | /tmp/grype | tee "${SECURITY_ARTIFACT_DIR}/cve.txt"
  emit_text_sbom "${BUILT_IMAGE}" | tee "${SECURITY_ARTIFACT_DIR}/sbom.txt"

  docker pull "${BASE_IMAGE}" >/dev/null
  emit_spdx_json "${BASE_IMAGE}" | /tmp/grype | tee "${SECURITY_ARTIFACT_DIR}/cve-base.txt"

  BASE_IMAGE_SHA="$(docker image inspect --format '{{index (split (index .RepoDigests 0) "@sha256:") 1}}' "${BASE_IMAGE}" 2>/dev/null || true)"
  if [[ -z "${BASE_IMAGE_SHA}" ]]; then
    BASE_IMAGE_SHA="$(docker image inspect --format '{{.Id}}' "${BASE_IMAGE}" 2>/dev/null | sed -E 's/^sha256://')"
  fi
  printf '%s\n' "${BASE_IMAGE_SHA}" > "${SECURITY_ARTIFACT_DIR}/base-image-sha.txt"

  exit 0
fi

OTHER_IMAGE_TAGS="${OTHER_IMAGE_TAGS} ${IMAGE_Z_TAG}"

curl -sSfL https://raw.githubusercontent.com/anchore/grype/main/install.sh | sh -s -- -b /tmp
curl -sSfL https://raw.githubusercontent.com/docker/sbom-cli-plugin/main/install.sh | sh -s --


docker sbom --format spdx-json ${BUILT_IMAGE} | /tmp/grype | tee /tmp/cve.txt
docker sbom ${BUILT_IMAGE} | tee /tmp/sbom.txt

docker sbom --format spdx-json ${BASE_IMAGE} | /tmp/grype | tee /tmp/cve-base.txt

docker login -u=$QUAY_TRAVIS_NOIROLABS_ROBO_USER -p=$QUAY_TRAVIS_NOIROLABS_ROBO_PSWD quay.io
docker push ${BUILT_IMAGE}

for OTHER_TAG in ${OTHER_IMAGE_TAGS}; do
 docker tag ${BUILT_IMAGE} ${QUAY_REGISTRY}/${IMAGE}:${OTHER_TAG}
 docker push ${QUAY_REGISTRY}/${IMAGE}:${OTHER_TAG}
done

docker login -u=$QUAY_TRAVIS_NOIRO_ROBO_USER -p=$QUAY_TRAVIS_NOIRO_ROBO_PSWD quay.io
docker tag ${BUILT_IMAGE} ${QUAY_NOIRO_REGISTRY}/${IMAGE}:${TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER}
docker push ${QUAY_NOIRO_REGISTRY}/${IMAGE}:${TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER}
docker tag ${BUILT_IMAGE} ${QUAY_NOIRO_REGISTRY}/${IMAGE}:${IMAGE_Z_TAG}
docker push ${QUAY_NOIRO_REGISTRY}/${IMAGE}:${IMAGE_Z_TAG}

docker login -u=$DOCKER_NOIRO_USER -p=$DOCKER_NOIRO_PSWD docker.io
docker tag ${BUILT_IMAGE} ${DOCKER_REGISTRY}/${IMAGE}:${TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER}
docker push ${DOCKER_REGISTRY}/${IMAGE}:${TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER}
docker tag ${BUILT_IMAGE} ${DOCKER_REGISTRY}/${IMAGE}:${IMAGE_Z_TAG}
docker push ${DOCKER_REGISTRY}/${IMAGE}:${IMAGE_Z_TAG}
