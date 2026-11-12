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
if [[ "${GITHUB_ACTIONS:-false}" == "true" && "${CI_EVIDENCE_MODE:-}" == "prepared-v1" ]]; then
  : "${CI_ARTIFACT_DIR:?CI_ARTIFACT_DIR is required}"
  : "${QUAY_NOIRO_DOCKER_CONFIG:?QUAY_NOIRO_DOCKER_CONFIG is required}"
  unset TRAVIS_TAGGER GITHUB_TOKEN GH_TOKEN PYPI_PASS TEST_PYPI_PASS
  exec python3 "$SCRIPTS_DIR/image_publication.py" \
    --artifacts "${CI_ARTIFACT_DIR}" --registry "${IMAGE_BUILD_REGISTRY}" \
    --image "${IMAGE}" --tag "${IMAGE_BUILD_TAG}" --other-tags "${OTHER_IMAGE_TAGS}" \
    --base-image "${BASE_IMAGE}" --z-tag "${IMAGE_Z_TAG}" \
    --dated-tag "${TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER}" \
    --quay-config "${QUAY_NOIRO_DOCKER_CONFIG}"
fi
BUILT_IMAGE=${IMAGE_BUILD_REGISTRY}/${IMAGE}:${IMAGE_BUILD_TAG}
OTHER_IMAGE_TAGS="${OTHER_IMAGE_TAGS} ${IMAGE_Z_TAG}"
GRYPE_BIN=${GRYPE_BIN:-/tmp/grype}
SYFT_BIN=${SYFT_BIN:-/tmp/syft}

# The child publisher has no reason to inherit the status-write credential. The
# caller's environment is unchanged for the legacy status entrypoint afterward.
if [[ "${GITHUB_ACTIONS:-false}" == "true" ]]; then
  unset TRAVIS_TAGGER GITHUB_TOKEN GH_TOKEN PYPI_PASS TEST_PYPI_PASS
fi

if [[ ! -x "${GRYPE_BIN}" ]]; then
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
    if [[ ! -x "${SYFT_BIN}" ]]; then
      curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s -- -b /tmp
    fi
    "${SYFT_BIN}" "${image_ref}" -o spdx-json
  fi
}

emit_text_sbom() {
  local image_ref=$1
  if docker sbom "${image_ref}" >/dev/null 2>&1; then
    docker sbom "${image_ref}"
  else
    if [[ ! -x "${SYFT_BIN}" ]]; then
      curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s -- -b /tmp
    fi
    "${SYFT_BIN}" "${image_ref}"
  fi
}

emit_spdx_json "${BUILT_IMAGE}" | "${GRYPE_BIN}" | tee /tmp/cve.txt
emit_text_sbom "${BUILT_IMAGE}" | tee /tmp/sbom.txt
emit_spdx_json "${BASE_IMAGE}" | "${GRYPE_BIN}" | tee /tmp/cve-base.txt

# The next image overwrites /tmp reports. Preserve this image's evidence now,
# even when a later push fails. Legacy Travis continues using the shared paths.
if [[ "${GITHUB_ACTIONS:-false}" == "true" ]]; then
  : "${CI_ARTIFACT_DIR:?CI_ARTIFACT_DIR is required}"
  REPORT_DIR="${CI_ARTIFACT_DIR}/reports/${IMAGE}"
  mkdir -p "${REPORT_DIR}"
  cp /tmp/sbom.txt "${REPORT_DIR}/sbom.txt"
  cp /tmp/cve.txt "${REPORT_DIR}/cve.txt"
  cp /tmp/cve-base.txt "${REPORT_DIR}/cve-base.txt"
fi

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

if [[ "${IMAGE}" == "opflex-build" ]]; then
  echo "Published opflex-build to ${QUAY_REGISTRY}; skipping Quay Noiro and Docker Hub publication."
  exit 0
fi

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

if [[ "${GITHUB_ACTIONS:-false}" == "true" && "${IMAGE}" != "opflex-build-base" ]]; then
  IMAGE_SHA=$(docker image inspect --format='{{.Id}}' "${BUILT_IMAGE}")
  python3 - "${CI_ARTIFACT_DIR}/image-metadata.jsonl" "${QUAY_NOIRO_REGISTRY}" \
    "${IMAGE}" "${IMAGE_BUILD_TAG}" "${OTHER_IMAGE_TAGS}" "${IMAGE_SHA}" \
    "${BASE_IMAGE}" "${IMAGE_Z_TAG}" "${TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER}" <<'PY'
import json
import sys

path, registry, image, tag, other_tags, sha, base_image, z_tag, dated_tag = sys.argv[1:]
record = {"registry": registry, "image": image, "tag": tag,
          "other_tags": other_tags.split(), "sha": sha.strip(), "base_image": base_image,
          "image_z_tag": z_tag, "dated_tag": dated_tag,
          "reports": {kind: "reports/" + image + "/" + kind + ".txt"
                      for kind in ("sbom", "cve", "cve-base")}}
with open(path, "a", encoding="utf-8") as stream:
    stream.write(json.dumps(record, sort_keys=True) + "\n")
PY
fi
