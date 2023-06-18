#!/bin/bash
set -x
git show --summary
git status
git branch
git tag --points-at HEAD
