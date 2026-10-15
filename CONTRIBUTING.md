# Contributing

Use a feature branch and a pull request. Keep credentials and private deployment details out of code, logs and issues. Bug and feature forms request reproducible examples; labels identify the affected server component.

## Development and validation

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]' build twine 'ruff>=0.14,<1'
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
.venv/bin/python scripts/verify-package.py
```

The normal suite uses local sockets and subprocess fixtures. Run real SSH tests with Docker, OpenSSH and `ssh-keygen` available:

```sh
TUNNEL_SWITCH_TEST_SSH=1 .venv/bin/python -m pytest -q -m ssh_integration
```

Explicit integration opt-in fails if required tools are missing. Tests create temporary keys, known-host entries and disposable SSH containers and clean up their own resources. Do not use personal SSH credentials.

## Review and branch protection

`main` requires a pull request, one approving review, resolved discussions, signed commits, current passing CI and CodeQL checks. Approvals are dismissed after new commits; the latest push requires approval by someone other than its author. GitHub does not let the PR author approve their own PR. Bot comments do not count as approval unless the bot submits an approving review accepted by GitHub. Copilot approvals count only when both auto-approval and approval counting are enabled in GitHub Settings → Copilot → Code review; comments do not count. CodeRabbit is configured to approve only after its latest review is complete, comments are resolved and pre-merge checks pass. A maintainer can review PRs authored by another contributor; a PR authored by the sole maintainer needs a qualifying external reviewer.

Use signed commits and squash merging. The branch rules block force pushes and deletion. The documented policy is stored in `.github/rulesets/`; changes to those JSON files must also be applied to GitHub. CODEOWNERS identifies the maintainer but does not add a second mandatory code-owner approval.

## Releases

Update `pyproject.toml` and the changelog through a reviewed PR. After main CI and CodeQL pass for the exact commit, create a signed `service-v<version>` tag on that commit and publish a matching GitHub release. The workflow checks version, main ancestry and successful checks, tests and builds distributions, then publishes the exact validated files through PyPI Trusted Publishing. Tags are protected against replacement and deletion.

Production publishing uses the `pypi` environment restricted to `service-v*` tags. Only the publishing job receives `id-token: write`. The verification job compares registry SHA256 values, installs the exact registry version, checks dependencies and runs the installed CLI with generated example configuration. Verified wheel, sdist, installation files and SHA256SUMS are attached to the GitHub release. The installation archive is reproducible. Existing release assets are accepted only when their hashes match; different files are never overwritten.

The PyPI publisher is configured for owner `timlaing`, repository `ssh-tunnel-switch`, workflow `publish-pypi.yml`, environment `pypi`. Versions cannot be overwritten. Upload retries skip existing filenames; registry hash verification must still match every validated artifact, so different content is rejected. If publication succeeded but verification failed, investigate and rerun only the failed verification job; do not rebuild and republish the same version.

## TestPyPI builds from main

A successful push CI run on `main`, including a merged PR, triggers `publish-testpypi.yml` for the exact tested commit. It assigns a unique `<release-version>.dev<build-serial>` version in the runner only. Each full workflow attempt gets a distinct serial; retrying only a failed publish job reuses its original validated artifacts. PR CI runs do not publish.

TestPyPI uses its separate account and publisher: project `ssh-tunnel-switch`, owner `timlaing`, repository `ssh-tunnel-switch`, workflow `publish-testpypi.yml` (not `.yaml`), environment `testpypi`. The GitHub environment is restricted to `main`. The workflow performs the same registry readback and installed CLI checks as production.

To try a test build, use an isolated environment. Resolve constrained dependencies from production PyPI using the corresponding source checkout or downloaded wheel, then install the exact development version from TestPyPI with `--no-deps`; avoid combining the two indexes:

```sh
python -m pip install .
python -m pip install --index-url https://test.pypi.org/simple/ --no-deps 'ssh-tunnel-switch==<development-version>'
python -m pip check
tunnel-switch --help
```

## Labels and maintenance

`.github/labels.json` defines custom labels. A main push changing that file synchronizes labels without deleting other labels. Issues receive `needs-triage`; PR file paths and conventional titles add component/type labels. Inactive issues are warned after 60 days and PRs after 30 days, then closed after 14 further days. `keep-open`, `security`, `priority: high`, milestones and draft PRs are exempt as configured in the stale workflow.

Dependabot checks Python and Actions dependencies weekly. Dependency audits and CodeQL also run weekly. Secret scanning, push protection and private vulnerability reporting are enabled; report vulnerabilities through the Security tab, not public issues.

## SonarQube analysis

The `SonarQube` workflow follows successful `CI` runs for main pushes and same-repository pull requests. Fork PRs still run unprivileged CI but do not receive credentialed scans; maintainers can move reviewed changes onto a branch in this repository to obtain a scan. CI uploads Python branch coverage; the scan downloads artifacts from that exact run and waits for the quality gate. It uses trusted workflow arguments, removes PR-controlled scanner properties, and does not execute repository code with the Sonar credential. `sonar-project.properties` documents the same source/test layout for local analysis.

Project: `timlaing_ssh-tunnel-switch`, organization: `timlaing`, service: `https://sonarcloud.io`. Enable GitHub variable `SONAR_ENABLED=true` and supply Actions secret `SONAR_TOKEN` only after the Sonar project exists with matching visibility. Disable Sonar automatic analysis when using this CI workflow. A missing credential fails an enabled scan; disabled scanning is explicitly skipped.

The public project is imported and CI scanning is enabled. Tokens are managed through GitHub Actions secrets; rotate them before the expiry recorded in `SONAR_TOKEN_EXPIRES_AT`.

CI installs binary-only dependencies from the hash-verified `requirements-ci.txt` lock and runs the checkout with `PYTHONPATH=src`; credentials are not exposed to test code. After changing dependencies, regenerate the universal CI lock with `uv pip compile pyproject.toml --extra test --group ci --universal --generate-hashes --output-file requirements-ci.txt` and include it in the PR.
