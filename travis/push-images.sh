#!/bin/bash
set -x
SCRIPTS_DIR=$(dirname ${BASH_SOURCE[0]})
source "$SCRIPTS_DIR/globals.sh"

IMAGE_BUILD_REGISTRY=$1
IMAGE=$2
IMAGE_BUILD_TAG=$3
OTHER_IMAGE_TAGS=${4//,/ }

BUILT_IMAGE=${IMAGE_BUILD_REGISTRY}/${IMAGE}:${IMAGE_BUILD_TAG}

curl -sSfL https://raw.githubusercontent.com/anchore/grype/main/install.sh | sh -s -- -b /tmp
curl -sSfL https://raw.githubusercontent.com/docker/sbom-cli-plugin/main/install.sh | sh -s --

docker sbom --format spdx-json ${BUILT_IMAGE} | /tmp/grype
docker sbom ${BUILT_IMAGE}

docker login -u=$QUAY_SUMIT_NOIROLABS_ROBO_USER -p=$QUAY_SUMIT_NOIROLABS_ROBO_PSWD quay.io
docker push ${BUILT_IMAGE}

for OTHER_TAG in ${OTHER_IMAGE_TAGS}; do
  docker tag ${BUILT_IMAGE} ${QUAY_REGISTRY}/${IMAGE}:${OTHER_TAG}
  docker push ${QUAY_REGISTRY}/${IMAGE}:${OTHER_TAG}
done
