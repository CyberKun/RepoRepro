# Contributing

Repo Repro is an early Python + pytest toolkit. Small, reviewable changes and reproducible bug reports are especially useful.

## Development setup

From a checkout, with Python 3.11+ and Git:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,qa]"
```

On PowerShell, activate with `.\.venv\Scripts\Activate.ps1` or use the executables in `.venv\Scripts` directly.

## Before opening a pull request

```bash
python -m pytest -m "not docker_integration"
python -m ruff check .
python -m ruff format --check .
python -m mypy
python -m build
python -m pip check
```

For sandbox, reproduction, verification, or Capsule changes, also run the real Docker tests. Prepare images using [the example instructions](examples/README.md#prepare-test-images), set `REPRO_DOCKER_TEST_IMAGE` and `REPRO_PYTEST_IMAGE` to the returned immutable IDs, then:

```bash
REPRO_REQUIRE_DOCKER=1 REPRO_REQUIRE_ENGINE=1 python -m pytest tests/integration
```

The two required-image flags turn missing prerequisites into failures. A default `pytest` run can skip Docker tests when images are not configured; disclose this in your PR. Do not use skipped tests or weaker assertions to conceal failures.

## Changes we can review

- Explain the observed problem, the change in behavior, and how you tested it.
- Add a regression test for a bug fix. Use an owned, minimal repository fixture.
- Keep unrelated refactors and formatting out of the patch.
- Preserve failure states, provenance, resource limits, and cleanup guarantees.
- Keep target execution inside the sandbox. Never execute an untrusted repository in a host-side test.
- Update the relevant example or documentation when changing a CLI or file format.

For larger features or format changes, open an issue with the proposed scope first. Current extension points do not imply that new languages or isolation backends are already implemented.

## Bug reports

Include the Repo Repro/Python/Docker versions, host platform, exact command, expected and observed behavior, and a minimal fixture if possible. Remove secrets and personal information. Private artifacts and Capsules can contain sensitive data; review them before attaching anything.

Report security vulnerabilities privately through [SECURITY.md](SECURITY.md). Community interactions follow our [Code of Conduct](CODE_OF_CONDUCT.md).

## Submissions and licensing

Contributions are provided under the project's [MIT License](LICENSE). Submit only work you have permission to share. Generated or assisted contributions are held to the same correctness, testing, licensing, and review standards as other work. Do not include tool transcripts, local QA dumps, model conversations, credentials, or unrelated generated files in a patch.
