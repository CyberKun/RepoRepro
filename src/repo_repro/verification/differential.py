"""Same sealed test, same image/policy, fresh containers, and deterministic verdict rules."""

from pathlib import Path
from uuid import uuid4

from repo_repro.analyzer.repository import GitRepository
from repo_repro.core.exceptions import InputError
from repo_repro.reproduction.executor import ReproductionExecutor
from repo_repro.reproduction.models import (
    DifferentialResult,
    DifferentialStatus,
    ExecutionEnvironment,
    GeneratedTest,
    ReproductionResult,
    ReproductionStatus,
)
from repo_repro.storage.artifacts import FileArtifactStore


def compare(
    original: ReproductionResult, fixed: ReproductionResult
) -> tuple[DifferentialStatus, str]:
    if (
        original.generated_test.sha256 != fixed.generated_test.sha256
        or original.environment != fixed.environment
    ):
        return DifferentialStatus.INCONCLUSIVE, "test or requested environment differs"
    if original.generated_test.oracle != fixed.generated_test.oracle:
        return DifferentialStatus.INCONCLUSIVE, "failure oracle differs"
    fingerprints = original.environment_fingerprints + fixed.environment_fingerprints
    if len(set(fingerprints)) > 1:
        return DifferentialStatus.ENVIRONMENT_ERROR, "actual runtime environments differ"
    if ReproductionStatus.ENVIRONMENT_ERROR in {original.status, fixed.status}:
        return (
            DifferentialStatus.ENVIRONMENT_ERROR,
            "at least one version has an environment failure",
        )
    if original.flaky or fixed.flaky:
        return DifferentialStatus.INCONCLUSIVE, "FLAKY: repetition is inconsistent"
    if original.status is ReproductionStatus.NOT_REPRODUCED:
        if fixed.status is ReproductionStatus.REPRODUCED:
            return (
                DifferentialStatus.REGRESSION_DETECTED,
                "target failure appeared only in the fixed version",
            )
        if fixed.status is ReproductionStatus.NOT_REPRODUCED:
            return DifferentialStatus.ORIGINAL_NOT_REPRODUCED, "original target passed all repeats"
    if original.status is ReproductionStatus.REPRODUCED:
        if fixed.status is ReproductionStatus.REPRODUCED:
            return (
                DifferentialStatus.FIXED_STILL_FAILS,
                "both versions reproduce the target failure",
            )
        if fixed.status is ReproductionStatus.NOT_REPRODUCED:
            if len(fingerprints) != original.environment.repetitions * 2 or not fingerprints:
                return DifferentialStatus.INCONCLUSIVE, "missing comparable runtime evidence"
            return (
                DifferentialStatus.DIFFERENTIAL_VALIDATED,
                "original target fails; fixed target passes all repeats",
            )
    return DifferentialStatus.INCONCLUSIVE, "unrelated failure, timeout, or incomplete evidence"


class DifferentialVerifier:
    def __init__(self, executor: ReproductionExecutor, artifacts: FileArtifactStore) -> None:
        self.executor = executor
        self.artifacts = artifacts

    def verify(
        self,
        repository_path: Path,
        original_commit: str,
        fixed_commit: str,
        reproduction_test: GeneratedTest,
        environment_config: ExecutionEnvironment,
    ) -> DifferentialResult:
        commits = []
        for ref in (original_commit, fixed_commit):
            with GitRepository().open(repository_path, ref=ref) as view:
                commits.append(view.commit)
        if commits[0] == commits[1]:
            raise InputError("original and fixed commits must differ")
        original = self.executor.run(
            repository_path, commits[0], reproduction_test, environment_config
        )
        fixed = self.executor.run(
            repository_path, commits[1], reproduction_test, environment_config
        )
        status, reason = compare(original, fixed)
        result = DifferentialResult(
            id=uuid4(),
            original_commit=commits[0],
            fixed_commit=commits[1],
            test_sha256=reproduction_test.sha256,
            status=status,
            reason=reason,
            original=original,
            fixed=fixed,
            environment_fingerprints=original.environment_fingerprints
            + fixed.environment_fingerprints,
        )
        self.artifacts.put_bytes(result.model_dump_json().encode(), media_type="application/json")
        return result
