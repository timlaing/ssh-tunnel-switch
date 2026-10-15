# Contributing

Use a feature branch and a pull request. Include validation results and keep credentials out of code, logs, and issues.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]' build twine
.venv/bin/python -m pytest -q
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
.venv/bin/tunnel-switch validate --config examples/config.yaml
```

Tests use local sockets and subprocess fixtures, without remote SSH accounts or JavaScript dependencies. Python 3.12+ and OpenSSH are required to run the server.

## Releases

Update the version in `pyproject.toml` and the changelog. Publish a GitHub release tagged `service-v<version>` from an approved commit. CI validates the package before the publish job uploads the exact artifacts to PyPI. Versions cannot be overwritten.

Before the first release, create a pending Trusted Publisher in your PyPI account with project name `ssh-tunnel-switch`, owner `timlaing`, repository `ssh-tunnel-switch`, workflow `publish-pypi.yml`, and environment `pypi`. Configure the GitHub `pypi` environment and any desired reviewer protection. No PyPI API token is needed. See https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/ . A pending publisher does not guarantee name availability; PyPI confirms acceptance on publication.

After publishing, verify the registry version and install `ssh-tunnel-switch==<version>` in a clean environment. Check `tunnel-switch --help` and example configuration validation before announcing the release.
