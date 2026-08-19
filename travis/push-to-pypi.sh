#!/bin/bash
if [[ "${GITHUB_ACTIONS:-false}" != "true" ]]; then
    set -x
fi
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
WHEEL_NAME="acc_provision-${PACKAGE_VERSION}.tar.gz"
TAG_NAME="${PACKAGE_VERSION}"
DEV_PACKAGE_VERSION="${PACKAGE_VERSION}.dev${TRAVIS_BUILD_NUMBER}"
DEV_WHEEL_NAME="acc_provision-${DEV_PACKAGE_VERSION}.tar.gz"
DEV_TAG_NAME="${DEV_PACKAGE_VERSION}"
TWINE_UPLOAD="true"

if [ -n "$PYPI_RELEASE" ] ; then
    python setup.py sdist
    python3 -m twine check "dist/${WHEEL_NAME}" || exit 1
    if ! TWINE_USERNAME="${PYPI_USER}" TWINE_PASSWORD="${PYPI_PASS}" \
        python3 -m twine upload --non-interactive "dist/${WHEEL_NAME}"; then
        echo "PyPI upload failed; CICD release status was not changed." >&2
        exit 1
    fi
    IS_RELEASE="true"
    if [[ "$TRAVIS_TAG" =~ $RC_REGEX ]]; then
        IS_RELEASE="false"
    fi
    push_cicd_status_if_enabled \
        "https://pypi.org/project/acc-provision/${TAG_NAME}/#files" \
        "${TAG_NAME}" "${IS_RELEASE}" "${TWINE_UPLOAD}" || exit 1

elif [ -n "$TEST_PYPI_RELEASE" ]; then
    if [[ "$TRAVIS_BUILD_USER" == "noiro-tagger" || "$TRAVIS_BUILD_USER" == "noiro-generic" ]]; then
        VERSION=${PACKAGE_VERSION}
        OVERRIDE_VERSION=${DEV_PACKAGE_VERSION}
        sed -i "s/${VERSION}/${OVERRIDE_VERSION}/" setup.py
        sed -i "s/${UPSTREAM_IMAGE_TAG}.*$/${UPSTREAM_IMAGE_Z_TAG}/" acc_provision/versions.yaml
        python setup.py sdist
        python3 -m twine check "dist/${DEV_WHEEL_NAME}" || exit 1
        if ! TWINE_USERNAME="${TEST_PYPI_USER}" TWINE_PASSWORD="${TEST_PYPI_PASS}" \
            python3 -m twine upload --non-interactive --repository testpypi \
            "dist/${DEV_WHEEL_NAME}"; then
            echo "TestPyPI upload failed; CICD release status was not changed." >&2
            exit 1
        fi
        push_cicd_status_if_enabled \
            "https://test.pypi.org/project/acc-provision/${OVERRIDE_VERSION}/#files" \
            "${DEV_TAG_NAME}" "false" "${TWINE_UPLOAD}" || exit 1
    elif [ -n "$SIGNED_EMAIL" ]; then
        python setup.py sdist
        python3 -m twine check "dist/${WHEEL_NAME}" || exit 1
        if ! TWINE_USERNAME="${TEST_PYPI_USER}" TWINE_PASSWORD="${TEST_PYPI_PASS}" \
            python3 -m twine upload --non-interactive --repository testpypi \
            "dist/${WHEEL_NAME}"; then
            echo "TestPyPI upload failed; CICD release status was not changed." >&2
            exit 1
        fi
        push_cicd_status_if_enabled \
            "https://test.pypi.org/project/acc-provision/${TAG_NAME}/#files" \
            "${TAG_NAME}" "false" "${TWINE_UPLOAD}" || exit 1
    fi
fi

popd
