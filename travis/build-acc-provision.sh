#!/bin/bash

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
#UPSTREAM_GIT_TAG=${VERSION}.${UPSTREAM_SHA}
sed -i "s/${VERSION}/${TRAVIS_TAG}/g" acc_provision/versions.yaml
popd

#python3 setup.py --description
#python3 setup.py sdist upload -r local
