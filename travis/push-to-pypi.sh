#!/bin/bash
set -x
SCRIPTS_DIR=$(dirname ${BASH_SOURCE[0]})
source "$SCRIPTS_DIR/globals.sh"

cat >~/.pypirc <<EOL
[distutils]
  index-servers =
    test-pypi

[test-pypi]
  repository: https://test.pypi.org/legacy/
  username: ashuparu
  password: ${PYPI_PASS}
EOL
cat ~/.pypirc
cd provision
python setup.py --description
#python setup.py sdist upload -r test-pypi
python setup.py sdist
twine upload --repository-url https://test.pypi.org/legacy/ -u ${PYPI_USER} -p ${PYPI_PASS} dist/*
