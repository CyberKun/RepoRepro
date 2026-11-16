# Reproduction and verification

Repo Repro performs bounded experiments: validate each candidate, run it repeatedly in fresh containers, and evaluate structured evidence. The one-shot generator remains available. The development branch also provides [AI investigation](ai-investigation.md), with bounded planning corrections and distinct candidate revisions based on original-version feedback. Project source and the caller's oracle stay fixed.

## Inputs

- `ReproductionInput`: Issue context, repository/environment facts, explicit source/test files, and a caller-owned `FailureOracle`.
- `FailureOracle`: expected exception type, required message fragment, Issue quotation, behavior contract, and fixed target node ID.
- `GeneratedTest`: test text and SHA-256, oracle, proposal, and generation/input artifact references.
- `ExecutionEnvironment`: approved image, local sandbox policy, and 2–10 repetitions; default 2.

`build_input()` reads the selected Git commit without checking out or executing project code. The oracle is fixed before model generation. A valid quotation proves that text was supplied, not that a test correctly captures its meaning; review the contract and assertion.

## Supported test shape

One `test_repro()` function, direct local result assignments, and one unconditional behavior assertion. The assertion must depend on a supplied repository call. Fixtures, markers, parametrization, annotations that execute at collection, control flow, result mutation, import rebinding, restricted builtins, and process/network/filesystem APIs are rejected.

AST checks reduce accidental unsafe or unrelated candidates. They cannot prove arbitrary Python safe or semantically correct. All imports and execution of repository/test code remain inside the sandbox.

## Evidence rules

The local launcher uses the image's pytest, disables auto-loaded plugins, repository conftest/configuration, and assertion rewriting, and loads the local collector. The report records collection, setup/call/teardown outcomes, exception type/message, traceback, skip/xfail state, session completion, and exit status.

Missing, duplicated, truncated, contradictory, skipped, or wrong-target evidence cannot establish reproduction. An ordinary nonzero exit is insufficient. The collector shares the target's process and container; hostile code can tamper with evidence. See [the security policy](../SECURITY.md).

| Single-version status | Meaning |
| --- | --- |
| `REPRODUCED` | Every repetition executes the target call and matches the sealed failure oracle. |
| `NOT_REPRODUCED` | Every repetition executes the target and passes. |
| `INCONCLUSIVE` | Unrelated failure, incomplete evidence, skip/xfail, or inconsistent repeats. |
| `ENVIRONMENT_ERROR` | Dependency, collection, fixture, infrastructure, enforcement, or environment failure. |
| `TIMEOUT` | Consistent termination at the sandbox deadline. |

Verification runs the exact same test, image, and policy on two distinct pinned commits. Runtime fingerprints compare Python/package inventory, image identity, helper hashes, and enforcement settings. Mixed outcomes or failure signatures mark a run flaky; finite stable repetitions do not prove determinism.

`DIFFERENTIAL_VALIDATED` requires stable target failure on the original and stable passing behavior on the fixed commit, with comparable runtime evidence. Other statuses include `ORIGINAL_NOT_REPRODUCED`, `FIXED_STILL_FAILS`, `REGRESSION_DETECTED`, `ENVIRONMENT_ERROR`, and `INCONCLUSIVE`. This verifies one target test, not the full project's regression suite.

## Optional model generation

Use `repro generate CONTEXT_JSON --model MODEL_ID`, optionally with `--base-url`. The provider uses OpenAI-compatible Chat Completions and validates the returned proposal locally. Set `OPENAI_API_KEY` through the environment. A local loopback HTTP provider requires explicit `--local-http`.

Selected Issue/source/test context is sent to the configured service. Audit artifacts record request/response details with the configured API credential redacted. They remain private evidence and should be reviewed before sharing. Model text cannot choose execution permissions, runtime images, or verification success.

The [complete owned demo](../examples/README.md) uses `--test-file` and requires no model or API key.
