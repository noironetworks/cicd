#!/bin/bash
set -x
REPO=$1
git config --local user.name "travis-tagger"
git config --local user.email "sumitnaiksatam+travis-tagger@gmail.com"
git remote add travis-tagger https://travis-tagger:$TRAVIS_TAGGER@github.com/noironetworks/$REPO.git
TAG_MESSAGE="ACI Release ${RELEASE_TAG} Created by Travis Job ${TRAVIS_JOB_NUMBER} ${TRAVIS_JOB_WEB_URL}"

git tag -f -a ${TRAVIS_TAG} -m "${TAG_MESSAGE}"; git push travis-tagger -f --tags
