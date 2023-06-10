#!/bin/bash
set -x
SCRIPTS_DIR=$(dirname ${BASH_SOURCE[0]})
source "$SCRIPTS_DIR/globals.sh"

TEST_PYPI_RELEASE_HINT="release to test.pypi.org"
PYPI_RELEASE_HINT="release to pypi.org"
TEST_PYPI_RELEASE=$(git show -s --format=%B  ${TRAVIS_TAG} | grep -i ${TEST_PYPI_RELEASE_HINT})
PYPI_RELEASE=$(git show -s --format=%B  ${TRAVIS_TAG} | grep -i ${PYPI_RELEASE_HINT})
if [ (-z ${TEST_PYPI_RELEASE+x}) && (-z ${PYPI_RELEASE+x}) ] ; then
    echo "To push to pypi, include ${TEST_PYPI_RELEASE_HINT} or ${PYPI_RELEASE_HINT} in git tag message, exiting"
    exit 0
fi
SIGNED_RELEASE=$(git tag -v ${TRAVIS_TAG} | grep -i "B6878A5BBF81C515428FA14E4CA0BB04A10CDFE1")
if [ -z ${USE_PYPI+x} ] ; then
    echo "Push to pypi only supported for tag signed by public key: B6878A5BBF81C515428FA14E4CA0BB04A10CDFE1 (sumitnaiksatam@gmail.com)"
    exit 1
fi
cd provision
python setup.py --description
python setup.py sdist
WHEEL_NAME="acc-provision-${TRAVIS_TAG}.tar.gz"
if [ -z ${PYPI_RELEASE_HINT} ] ; then
    twine upload --repository-url https://pypi.org/legacy/ -u ${PYPI_USER} -p ${PYPI_PASS} dist/$WHEEL_NAME
elif [ -z ${TEST_PYPI_RELEASE_HINT} ] ; then
    twine upload --repository-url https://test.pypi.org/legacy/ -u ${TEST_PYPI_USER} -p ${TEST_PYPI_PASS} dist/$WHEEL_NAME
fi
