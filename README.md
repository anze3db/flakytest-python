# flakytest-python

[![PyPI - Version](https://img.shields.io/pypi/v/flakytest.svg)](https://pypi.org/project/flakytest)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/flakytest.svg)](https://pypi.org/project/flakytest)

-----

**Table of Contents**

- [Installation](#installation)
- [License](#license)

## Installation

```console
pip install flakytest --pre
```

Obtain the `FLAKYTEST_SECRET_TOKEN` from flakytest.com and set it as an environment variable (e.g. your secrets store in your CI/CD pipeline)

```console
export FLAKYTEST_SECRET_TOKEN=****************************
```

Run your tests with `pytest` normally

```console
pytest
```

## License

`flakytest` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html) license.
