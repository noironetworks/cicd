#!/bin/bash

cat >~/.pypirc <<EOL
[distutils]
  index-servers =
    test-pypi

[test-pypi]
  repository: https://test.pypi.org/legacy/
  username: __token__
  password: ${PYPI_PASS}
EOL

python setup.py --description
python setup.py sdist upload -r test-pypi
