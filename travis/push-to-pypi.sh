#!/bin/bash
set -x
SCRIPTS_DIR=$(dirname ${BASH_SOURCE[0]})
source "$SCRIPTS_DIR/globals.sh"

cd provision
python setup.py --description
#python setup.py sdist upload -r test-pypi
python setup.py sdist
twine upload --repository-url https://test.pypi.org/legacy/ -u ${PYPI_USER} -p ${PYPI_PASS} dist/*
