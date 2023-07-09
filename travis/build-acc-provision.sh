#!/bin/bash
set -x
SCRIPTS_DIR=$(dirname ${BASH_SOURCE[0]})
source "$SCRIPTS_DIR/globals.sh"

#ACI_CONTAINERS_DIR=/tmp/aci-containers
#export ACI_CONTAINERS_DIR
#git clone https://github.com/noironetworks/aci-containers.git -b ${TRAVIS_TAG} $ACI_CONTAINERS_DIR

#pushd ${ACI_CONTAINERS_DIR}
#make -C ${ACI_CONTAINERS_DIR} all-static
#cp ${ACI_CONTAINERS_DIR}/dist-static/acikubectl provision/bin/acikubectl
#popd

pushd provision
VERSION=`python3 setup.py --version`
OVERRIDE_VERSION=${TRAVIS_TAG}
sed -i "s/${VERSION}/${OVERRIDE_VERSION}/" setup.py
if [[ "$TRAVIS_TAG" = "$RELEASE_TAG" ]]; then
    echo "Travis tag matches release tag ${RELEASE_TAG}, preserving image tags in versions.yaml"
    cat acc_provision/versions.yaml | grep ${RELEASE_TAG}
    exit 0
fi
if [[ "$TRAVIS_TAG" =~ $RC_REGEX ]]; then
    RC_NUM=$(echo "$TRAVIS_TAG" | sed "s/${RC_PREFIX}//")
    OVERRIDE_TAG=${UPSTREAM_IMAGE_TAG}.rc${RC_NUM}
    sed -i "s/${UPSTREAM_IMAGE_TAG}/${OVERRIDE_TAG}/g" acc_provision/versions.yaml
    cat acc_provision/versions.yaml | grep ${RELEASE_TAG}
    exit 0
fi

export UPSTREAM_IMAGE_Z_TAG

ACC_PROVISION_OPERATOR_VERSION="acc_provision_operator_version: "${UPSTREAM_IMAGE_TAG}
OTHER_TAG_FOR_Z_TAG=`curl -L -s 'https://quay.io/api/v1/repository/noiro/acc-provision-operator/tag/' | jq -r ' . as $parent | ( .tags[] | select(.name==$ENV.UPSTREAM_IMAGE_Z_TAG) | .manifest_digest as $digest | ($parent.tags[] | select((.manifest_digest==$digest) and .name!=$ENV.UPSTREAM_IMAGE_Z_TAG) | .name ) )'`
NEW_ACC_PROVISION_OPERATOR_VERSION="acc_provision_operator_version: "${OTHER_TAG_FOR_Z_TAG}
sed -i "s/${ACC_PROVISION_OPERATOR_VERSION}/${NEW_ACC_PROVISION_OPERATOR_VERSION}/" acc_provision/versions.yaml

ACI_CONTAINERS_CONTROLLER_VERSION="aci_containers_controller_version: "${UPSTREAM_IMAGE_TAG}
OTHER_TAG_FOR_Z_TAG=`curl -L -s 'https://quay.io/api/v1/repository/noiro/aci-containers-controller/tag/' | jq -r ' . as $parent | ( .tags[] | select(.name==$ENV.UPSTREAM_IMAGE_Z_TAG) | .manifest_digest as $digest | ($parent.tags[] | select((.manifest_digest==$digest) and .name!=$ENV.UPSTREAM_IMAGE_Z_TAG) | .name ) )'`
NEW_ACI_CONTAINERS_CONTROLLER_VERSION="aci_containers_controller_version: "${OTHER_TAG_FOR_Z_TAG}
sed -i "s/${ACI_CONTAINERS_CONTROLLER_VERSION}/${NEW_ACI_CONTAINERS_CONTROLLER_VERSION}/" acc_provision/versions.yaml

ACI_CONTAINERS_HOST_VERSION="aci_containers_host_version: "${UPSTREAM_IMAGE_TAG}
OTHER_TAG_FOR_Z_TAG=`curl -L -s 'https://quay.io/api/v1/repository/noiro/aci-containers-host/tag/' | jq -r ' . as $parent | ( .tags[] | select(.name==$ENV.UPSTREAM_IMAGE_Z_TAG) | .manifest_digest as $digest | ($parent.tags[] | select((.manifest_digest==$digest) and .name!=$ENV.UPSTREAM_IMAGE_Z_TAG) | .name ) )'`
NEW_ACI_CONTAINERS_HOST_VERSION="aci_containers_host_version: "${OTHER_TAG_FOR_Z_TAG}
sed -i "s/${ACI_CONTAINERS_HOST_VERSION}/${NEW_ACI_CONTAINERS_HOST_VERSION}/" acc_provision/versions.yaml

ACI_CONTAINERS_OPERATOR_VERSION="aci_containers_operator_version: "${UPSTREAM_IMAGE_TAG}
OTHER_TAG_FOR_Z_TAG=`curl -L -s 'https://quay.io/api/v1/repository/noiro/aci-containers-operator/tag/' | jq -r ' . as $parent | ( .tags[] | select(.name==$ENV.UPSTREAM_IMAGE_Z_TAG) | .manifest_digest as $digest | ($parent.tags[] | select((.manifest_digest==$digest) and .name!=$ENV.UPSTREAM_IMAGE_Z_TAG) | .name ) )'`
NEW_ACI_CONTAINERS_OPERATOR_VERSION="aci_containers_operator_version: "${OTHER_TAG_FOR_Z_TAG}
sed -i "s/${ACI_CONTAINERS_OPERATOR_VERSION}/${NEW_ACI_CONTAINERS_OPERATOR_VERSION}/" acc_provision/versions.yaml

CNIDEPLOY_VERSION="cnideploy_version: "${UPSTREAM_IMAGE_TAG}
OTHER_TAG_FOR_Z_TAG=`curl -L -s 'https://quay.io/api/v1/repository/noiro/cnideploy/tag/' | jq -r ' . as $parent | ( .tags[] | select(.name==$ENV.UPSTREAM_IMAGE_Z_TAG) | .manifest_digest as $digest | ($parent.tags[] | select((.manifest_digest==$digest) and .name!=$ENV.UPSTREAM_IMAGE_Z_TAG) | .name ) )'`
NEW_CNIDEPLOY_VERSION="cnideploy_version: "${OTHER_TAG_FOR_Z_TAG}
sed -i "s/${CNIDEPLOY_VERSION}/${NEW_CNIDEPLOY_VERSION}/" acc_provision/versions.yaml

OPENVSWITCH_VERSION="openvswitch_version: "${UPSTREAM_IMAGE_TAG}
OTHER_TAG_FOR_Z_TAG=`curl -L -s 'https://quay.io/api/v1/repository/noiro/openvswitch/tag/' | jq -r ' . as $parent | ( .tags[] | select(.name==$ENV.UPSTREAM_IMAGE_Z_TAG) | .manifest_digest as $digest | ($parent.tags[] | select((.manifest_digest==$digest) and .name!=$ENV.UPSTREAM_IMAGE_Z_TAG) | .name ) )'`
NEW_OPENVSWITCH_VERSION="openvswitch_version: "${OTHER_TAG_FOR_Z_TAG}
sed -i "s/${OPENVSWITCH_VERSION}/${NEW_OPENVSWITCH_VERSION}/" acc_provision/versions.yaml

OPFLEX_AGENT_VERSION="opflex_agent_version: "${UPSTREAM_IMAGE_TAG}
OTHER_TAG_FOR_Z_TAG=`curl -L -s 'https://quay.io/api/v1/repository/noiro/opflex/tag/' | jq -r ' . as $parent | ( .tags[] | select(.name==$ENV.UPSTREAM_IMAGE_Z_TAG) | .manifest_digest as $digest | ($parent.tags[] | select((.manifest_digest==$digest) and .name!=$ENV.UPSTREAM_IMAGE_Z_TAG) | .name ) )'`
NEW_OPFLEX_AGENT_VERSION="opflex_agent_version: "${OTHER_TAG_FOR_Z_TAG}
sed -i "s/${OPFLEX_AGENT_VERSION}/${NEW_OPFLEX_AGENT_VERSION}/" acc_provision/versions.yaml

cat acc_provision/versions.yaml | grep ${RELEASE_TAG}

popd

#python3 setup.py --description
#python3 setup.py sdist upload -r local
