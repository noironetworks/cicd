#!/bin/bash
SCRIPTS_DIR=$(dirname ${BASH_SOURCE[0]})
source "$SCRIPTS_DIR/globals.sh"

echo "The tag ${UPSTREAM_IMAGE_TAG} will be replaced by the following tags:"

export UPSTREAM_IMAGE_Z_TAG

OTHER_TAG_FOR_Z_TAG=`curl -L -s 'https://quay.io/api/v1/repository/noiro/acc-provision-operator/tag/' | jq -r ' . as $parent | ( .tags[] | select(.name==$ENV.UPSTREAM_IMAGE_Z_TAG) | .manifest_digest as $digest | ($parent.tags[] | select((.manifest_digest==$digest) and .name!=$ENV.UPSTREAM_IMAGE_Z_TAG) | .name ) )'`
echo "acc-provision-operator: "$OTHER_TAG_FOR_Z_TAG

OTHER_TAG_FOR_Z_TAG=`curl -L -s 'https://quay.io/api/v1/repository/noiro/aci-containers-controller/tag/' | jq -r ' . as $parent | ( .tags[] | select(.name==$ENV.UPSTREAM_IMAGE_Z_TAG) | .manifest_digest as $digest | ($parent.tags[] | select((.manifest_digest==$digest) and .name!=$ENV.UPSTREAM_IMAGE_Z_TAG) | .name ) )'`
echo "aci-containers-controller: "$OTHER_TAG_FOR_Z_TAG

OTHER_TAG_FOR_Z_TAG=`curl -L -s 'https://quay.io/api/v1/repository/noiro/aci-containers-host/tag/' | jq -r ' . as $parent | ( .tags[] | select(.name==$ENV.UPSTREAM_IMAGE_Z_TAG) | .manifest_digest as $digest | ($parent.tags[] | select((.manifest_digest==$digest) and .name!=$ENV.UPSTREAM_IMAGE_Z_TAG) | .name ) )'`
echo "aci-containers-host: "$OTHER_TAG_FOR_Z_TAG

OTHER_TAG_FOR_Z_TAG=`curl -L -s 'https://quay.io/api/v1/repository/noiro/aci-containers-operator/tag/' | jq -r ' . as $parent | ( .tags[] | select(.name==$ENV.UPSTREAM_IMAGE_Z_TAG) | .manifest_digest as $digest | ($parent.tags[] | select((.manifest_digest==$digest) and .name!=$ENV.UPSTREAM_IMAGE_Z_TAG) | .name ) )'`
echo "aci-containers-operator: "$OTHER_TAG_FOR_Z_TAG

OTHER_TAG_FOR_Z_TAG=`curl -L -s 'https://quay.io/api/v1/repository/noiro/cnideploy/tag/' | jq -r ' . as $parent | ( .tags[] | select(.name==$ENV.UPSTREAM_IMAGE_Z_TAG) | .manifest_digest as $digest | ($parent.tags[] | select((.manifest_digest==$digest) and .name!=$ENV.UPSTREAM_IMAGE_Z_TAG) | .name ) )'`
echo "cnideploy: "$OTHER_TAG_FOR_Z_TAG

OTHER_TAG_FOR_Z_TAG=`curl -L -s 'https://quay.io/api/v1/repository/noiro/openvswitch/tag/' | jq -r ' . as $parent | ( .tags[] | select(.name==$ENV.UPSTREAM_IMAGE_Z_TAG) | .manifest_digest as $digest | ($parent.tags[] | select((.manifest_digest==$digest) and .name!=$ENV.UPSTREAM_IMAGE_Z_TAG) | .name ) )'`
echo "openvswitch: "$OTHER_TAG_FOR_Z_TAG

OTHER_TAG_FOR_Z_TAG=`curl -L -s 'https://quay.io/api/v1/repository/noiro/opflex/tag/' | jq -r ' . as $parent | ( .tags[] | select(.name==$ENV.UPSTREAM_IMAGE_Z_TAG) | .manifest_digest as $digest | ($parent.tags[] | select((.manifest_digest==$digest) and .name!=$ENV.UPSTREAM_IMAGE_Z_TAG) | .name ) )'`
echo "opflex: "$OTHER_TAG_FOR_Z_TAG
