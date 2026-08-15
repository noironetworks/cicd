#!/bin/bash
set -x
SCRIPTS_DIR=$(dirname ${BASH_SOURCE[0]})
source "$SCRIPTS_DIR/globals.sh"

TEST_PYPI_RELEASE_HINT="release to test.pypi.org"
PYPI_RELEASE_HINT="release to pypi.org"
TEST_PYPI_RELEASE=$(git show -s --format=%B ${TRAVIS_TAG} | grep -i "${TEST_PYPI_RELEASE_HINT}")
PYPI_RELEASE=$(git show -s --format=%B  ${TRAVIS_TAG} | grep -i "${PYPI_RELEASE_HINT}")

push_cicd_status_if_enabled() {
    if [[ "${SKIP_CICD_STATUS:-false}" == "true" ]]; then
        return 0
    fi
    "$SCRIPTS_DIR/push-to-cicd-status.sh" "$@"
}

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

PACKAGE_VERSION="${TRAVIS_TAG}"
if [[ "${GITHUB_ACTIONS:-false}" == "true" && ! "$TRAVIS_TAG" =~ $RC_REGEX ]]; then
    PACKAGE_VERSION="${RELEASE_TAG}"
fi
WHEEL_NAME="acc_provision-${PACKAGE_VERSION}.tar.gz"
TAG_NAME="${PACKAGE_VERSION}"
DEV_PACKAGE_VERSION="${PACKAGE_VERSION}.dev${TRAVIS_BUILD_NUMBER}"
DEV_WHEEL_NAME="acc_provision-${DEV_PACKAGE_VERSION}.tar.gz"
DEV_TAG_NAME="${DEV_PACKAGE_VERSION}"
TWINE_UPLOAD="true"
UPLOAD_ATTEMPTED="false"

if [ -n "$PYPI_RELEASE" ] ; then
    #twine upload --repository-url https://pypi.org/legacy/ -u ${PYPI_USER} -p ${PYPI_PASS} dist/$WHEEL_NAME
    python setup.py sdist
    UPLOAD_ATTEMPTED="true"
    twine upload --verbose -u ${PYPI_USER} -p ${PYPI_PASS} dist/$WHEEL_NAME
    if [ $? -ne 0 ]; then
        TWINE_UPLOAD="false"
    fi
    IS_RELEASE="true"
    if [[ "$TRAVIS_TAG" =~ $RC_REGEX ]]; then
        IS_RELEASE="false"
    fi
    push_cicd_status_if_enabled "https://pypi.org/project/acc-provision/"${TAG_NAME}"/#files" "${TAG_NAME}" ${IS_RELEASE} ${TWINE_UPLOAD}

elif [ -n "$TEST_PYPI_RELEASE" ]; then
    if [[ "$TRAVIS_BUILD_USER" == "noiro-tagger" || "$TRAVIS_BUILD_USER" == "noiro-generic" ]]; then
        #twine upload --repository-url https://test.pypi.org/legacy/ -u ${TEST_PYPI_USER} -p ${TEST_PYPI_PASS} dist/$DEV_WHEEL_NAME
        VERSION=${PACKAGE_VERSION}
        OVERRIDE_VERSION=${DEV_PACKAGE_VERSION}
        sed -i "s/${VERSION}/${OVERRIDE_VERSION}/" setup.py
        sed -i "s/${UPSTREAM_IMAGE_TAG}.*$/${UPSTREAM_IMAGE_Z_TAG}/" acc_provision/versions.yaml
        python setup.py sdist
        UPLOAD_ATTEMPTED="true"
        twine upload --verbose --repository testpypi -u ${TEST_PYPI_USER} -p ${TEST_PYPI_PASS} dist/$DEV_WHEEL_NAME
        if [ $? -ne 0 ]; then
            TWINE_UPLOAD="false"
        fi
        push_cicd_status_if_enabled "https://test.pypi.org/project/acc-provision/"${OVERRIDE_VERSION}"/#files" "${DEV_TAG_NAME}" "false" ${TWINE_UPLOAD}
    elif [ -n "$SIGNED_EMAIL" ]; then
        python setup.py sdist
        UPLOAD_ATTEMPTED="true"
        twine upload --verbose --repository testpypi -u ${TEST_PYPI_USER} -p ${TEST_PYPI_PASS} dist/$WHEEL_NAME
        if [ $? -ne 0 ]; then
            TWINE_UPLOAD="false"
        fi
        push_cicd_status_if_enabled "https://test.pypi.org/project/acc-provision/"${TAG_NAME}"/#files" "${TAG_NAME}" "false" ${TWINE_UPLOAD}
    fi
fi

popd

if [[ "${UPLOAD_ATTEMPTED}" == "true" && "${TWINE_UPLOAD}" != "true" ]]; then
    echo "Twine upload failed. See verbose output above for the exact TestPyPI/PyPI rejection reason."
    exit 1
fi
