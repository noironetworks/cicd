#!/bin/bash
set -x
SCRIPTS_DIR=$(dirname ${BASH_SOURCE[0]})
source "$SCRIPTS_DIR/globals.sh"

# Clone the aci-containers repository with specified depth and branch
git clone --depth=50 --branch=${RELEASE_TAG} https://github.com/noironetworks/aci-containers.git /tmp/noironetworks/aci-containers