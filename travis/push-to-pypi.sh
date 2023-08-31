#!/bin/bash
set -x
SCRIPTS_DIR=$(dirname ${BASH_SOURCE[0]})
source "$SCRIPTS_DIR/globals.sh"

TEST_PYPI_RELEASE_HINT="release to test.pypi.org"
PYPI_RELEASE_HINT="release to pypi.org"
TEST_PYPI_RELEASE=$(git show -s --format=%B ${TRAVIS_TAG} | grep -i "${TEST_PYPI_RELEASE_HINT}")
PYPI_RELEASE=$(git show -s --format=%B  ${TRAVIS_TAG} | grep -i "${PYPI_RELEASE_HINT}")

if [[ (-z "$TEST_PYPI_RELEASE") && (-z "$PYPI_RELEASE") ]] ; then
    echo "To push to pypi, include ${TEST_PYPI_RELEASE_HINT} or ${PYPI_RELEASE_HINT} in git tag message, exiting"
    exit 0
fi

SIGNED_RELEASE=$(git tag -v ${TRAVIS_TAG} 2>&1 | grep -i "B6878A5BBF81C515428FA14E4CA0BB04A10CDFE1")

SIGNED_EMAIL=$(git tag -v ${TRAVIS_TAG} 2>&1 | grep -i "sumitnaiksatam@gmail.com")

# Check if it is a pypi release and not a signed release then exit
if [ -n "$PYPI_RELEASE" ] && [ -z "$SIGNED_RELEASE" ] ; then
    echo "Push to pypi only supported for tag signed by public key: B6878A5BBF81C515428FA14E4CA0BB04A10CDFE1 (sumitnaiksatam@gmail.com)"
    exit 1
fi

pushd provision
python setup.py --description

WHEEL_NAME="acc_provision-${TRAVIS_TAG}.tar.gz"
TAG_NAME="acc_provision-${TRAVIS_TAG}"
DEV_WHEEL_NAME="acc_provision-${TRAVIS_TAG}.dev${TRAVIS_BUILD_NUMBER}.tar.gz"
DEV_TAG_NAME="acc_provision-${TRAVIS_TAG}.dev${TRAVIS_BUILD_NUMBER}"
TWINE_UPLOAD="true"

if [ -n "$PYPI_RELEASE" ] ; then
    #twine upload --repository-url https://pypi.org/legacy/ -u ${PYPI_USER} -p ${PYPI_PASS} dist/$WHEEL_NAME
    python setup.py sdist
    twine upload -u ${PYPI_USER} -p ${PYPI_PASS} dist/$WHEEL_NAME
    if [ $? -ne 0 ]; then
        TWINE_UPLOAD="false"
    fi
    $SCRIPTS_DIR/push-to-cicd-status.sh "https://pypi.org/project/acc-provision/"${TRAVIS_TAG}"/#files" "${TAG_NAME}" "true" ${TWINE_UPLOAD}
elif [ -n "$TEST_PYPI_RELEASE" ]; then
    if [ "$TRAVIS_BUILD_USER" == "noiro-tagger" ]; then
        #twine upload --repository-url https://test.pypi.org/legacy/ -u ${TEST_PYPI_USER} -p ${TEST_PYPI_PASS} dist/$DEV_WHEEL_NAME
        VERSION=${TRAVIS_TAG}
        OVERRIDE_VERSION=${TRAVIS_TAG}.dev${TRAVIS_BUILD_NUMBER}
        sed -i "s/${VERSION}/${OVERRIDE_VERSION}/" setup.py
        sed -i "s/${UPSTREAM_IMAGE_TAG}.*$/${UPSTREAM_IMAGE_Z_TAG}/" acc_provision/versions.yaml
        python setup.py sdist
        twine upload --repository testpypi -u ${TEST_PYPI_USER} -p ${TEST_PYPI_PASS} dist/$DEV_WHEEL_NAME
        if [ $? -ne 0 ]; then
            TWINE_UPLOAD="false"
        fi
        $SCRIPTS_DIR/push-to-cicd-status.sh "https://test.pypi.org/project/acc-provision/"${OVERRIDE_VERSION}"/#files" "${DEV_TAG_NAME}" "false" ${TWINE_UPLOAD}
    elif [ -n "$SIGNED_EMAIL" ]; then
        python setup.py sdist
        twine upload --repository testpypi -u ${TEST_PYPI_USER} -p ${TEST_PYPI_PASS} dist/$WHEEL_NAME
        if [ $? -ne 0 ]; then
            TWINE_UPLOAD="false"
        fi
        $SCRIPTS_DIR/push-to-cicd-status.sh "https://test.pypi.org/project/acc-provision/"${TRAVIS_TAG}"/#files" "${TAG_NAME}" "false" ${TWINE_UPLOAD}
    fi
fi

popd