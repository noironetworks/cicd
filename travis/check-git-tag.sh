#!/bin/bash
set -x

git show --summary

EXPECTED_TAG_PREFIX="6.0.3.2"

if [[ "${TRAVIS_TAG}" != "${EXPECTED_TAG_PREFIX}"* ]] ; then
    echo "The applied git tag " ${TRAVIS_TAG} " did not match the expected tag prefix " ${EXPECTED_TAG_PREFIX} ". Skipping building."
    exit 140
fi

tag_message=$(git show -s --format=%B  ${TRAVIS_TAG} | grep "Created by Travis Job")
if [[ ! -z ${tag_message} ]] ; then
    echo "This job is triggered by git tag " ${TRAVIS_TAG} " that was applied by a preceeding Travis job, hence no further processing is required."
    exit 140
fi
