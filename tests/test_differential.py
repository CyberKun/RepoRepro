"""Complete deterministic status matrix and flaky detection without requiring Docker."""

import hashlib
import json
from pathlib import Path

import pytest

from repo_repro.core.exceptions import ExecutionBackendError, PolicyViolation, SandboxError
from repo_repro.core.models import CommandSpec, ExecutionResult, ResourceUsage, Termination
from repo_repro.reproduction.executor import ReproductionExecutor
from repo_repro.reproduction.generator import TestGenerator
from repo_repro.reproduction.models import (
    DifferentialStatus,
    ExecutionEnvironment,
    PytestRunRequest,
    ReproductionStatus,
)
from repo_repro.reproduction.planner import build_input
from repo_repro.reproduction.runtime import injected_files
from repo_repro.storage.artifacts import FileArtifactStore
from repo_repro.verification.differential import DifferentialVerifier
from tests.integration.test_reproduction import repository_pair
from tests.repro_support import CODE, context, execution, report

IMAGE = "sha256:" + "c" * 64


class ScriptedBackend:
    def __init__(self, store: FileArtifactStore, script: list[str], *, drift: bool = False) -> None:
        self.store = store
        self.script = script
        self.calls: list[PytestRunRequest] = []
        self.drift = drift

    def run_test(self, request: PytestRunRequest) -> ExecutionResult:
        self.calls.append(request)
        action = self.script[len(self.calls) - 1]
        if action == "backend_error":
            raise SandboxError("fixture backend unavailable")
        passed = action == "pass"
        evidence = report(passed=passed)
        code = 0 if passed else 1
        termination = Termination.EXITED
        if action == "wrong":
            evidence = report(exception_type="ValueError", message="unrelated input")
        elif action == "environment":
            code = 4
        elif action == "timeout":
            code, termination = 137, Termination.TIMEOUT
        data = {
            "cleanup_confirmed": True,
            "image_reference": IMAGE,
            "image_id": ("sha256:" + "d" * 64) if self.drift and len(self.calls) > 2 else IMAGE,
            "policy": request.policy.model_dump(),
            "test_sha256": hashlib.sha256(request.test_code.encode()).hexdigest(),
            "injected_files": [
                (name, hashlib.sha256(content).hexdigest())
                for name, content in injected_files(request.test_code).items()
            ],
            "cgroup_probe": {
                "python_version": "3.11.0",
                "packages": [["pytest", "9.2.0"]],
                "memory": "2147483648",
                "swap": "0",
                "cpu": "100000 100000",
                "pids": "128",
            },
        }
        result = execution(code, termination)
        return result.model_copy(
            update={
                "run_id": request.run_id,
                "plan_id": request.plan_id,
                "repetition": request.repetition,
                "revision": result.revision.model_copy(update={"commit": request.commit}),
                "command": CommandSpec(
                    argv=("python", "-I", "/workspace/repo_repro_generated/launch.py")
                ),
                "backend_snapshot": self.store.put_bytes(
                    json.dumps(data).encode(), media_type="application/json"
                ),
                "pytest_report": self.store.put_bytes(
                    evidence.model_dump_json().encode(), media_type="application/json"
                ),
                "resource_usage": ResourceUsage(
                    memory_limit_bytes=2147483648, cpu_limit=1, pids_limit=128, cgroup_verified=True
                ),
            }
        )


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        (["target", "target", "pass", "pass"], DifferentialStatus.DIFFERENTIAL_VALIDATED),
        (["pass", "pass", "target", "target"], DifferentialStatus.REGRESSION_DETECTED),
        (["pass", "pass", "pass", "pass"], DifferentialStatus.ORIGINAL_NOT_REPRODUCED),
        (["target", "target", "target", "target"], DifferentialStatus.FIXED_STILL_FAILS),
        (["target", "target", "wrong", "wrong"], DifferentialStatus.INCONCLUSIVE),
        (["environment", "environment", "pass", "pass"], DifferentialStatus.ENVIRONMENT_ERROR),
        (["timeout", "timeout", "pass", "pass"], DifferentialStatus.INCONCLUSIVE),
        (["target", "pass", "pass", "pass"], DifferentialStatus.INCONCLUSIVE),
    ],
)
def test_differential_statuses_from_independent_evidence(
    tmp_path: Path,
    script: list[str],
    expected: DifferentialStatus,
) -> None:
    source = tmp_path / "repo"
    original, fixed = repository_pair(source)
    store = FileArtifactStore(tmp_path / "evidence")
    data = build_input(
        context().issue,
        source,
        context().oracle,
        ref=original,
        source_paths=("calculator.py",),
        test_paths=("tests/test_calculator.py",),
    )
    generated = TestGenerator(store).seal_code(data, CODE)
    backend = ScriptedBackend(store, script)
    environment = ExecutionEnvironment.model_validate(
        {"image": IMAGE, "policy": {"isolation": "reviewed_docker"}}
    )
    result = DifferentialVerifier(ReproductionExecutor(backend, store), store).verify(
        source,
        original,
        fixed,
        generated,
        environment,
    )
    assert result.status is expected
    assert len(backend.calls) == 4
    assert all(call.test_code == CODE for call in backend.calls)
    assert result.original_commit == original and result.fixed_commit == fixed
    if script[:2] == ["target", "pass"]:
        assert result.original.flaky


def test_actual_environment_drift_prevents_validation(tmp_path: Path) -> None:
    source = tmp_path / "repo"
    original, fixed = repository_pair(source)
    store = FileArtifactStore(tmp_path / "evidence")
    generated = TestGenerator(store).seal_code(context(), CODE)
    backend = ScriptedBackend(store, ["target", "target", "pass", "pass"], drift=True)
    environment = ExecutionEnvironment.model_validate(
        {"image": IMAGE, "policy": {"isolation": "reviewed_docker"}}
    )
    result = DifferentialVerifier(ReproductionExecutor(backend, store), store).verify(
        source, original, fixed, generated, environment
    )
    assert result.status is DifferentialStatus.ENVIRONMENT_ERROR


def test_changed_code_is_refused_before_any_execution(tmp_path: Path) -> None:
    source = tmp_path / "repo"
    original, _ = repository_pair(source)
    store = FileArtifactStore(tmp_path / "evidence")
    generated = (
        TestGenerator(store).seal_code(context(), CODE).model_copy(update={"sha256": "0" * 64})
    )
    backend = ScriptedBackend(store, [])
    environment = ExecutionEnvironment.model_validate(
        {"image": IMAGE, "policy": {"isolation": "reviewed_docker"}}
    )
    with pytest.raises(PolicyViolation):
        ReproductionExecutor(backend, store).run(source, original, generated, environment)
    assert not backend.calls


def test_backend_exception_is_an_environment_error_and_is_recorded(tmp_path: Path) -> None:
    source = tmp_path / "repo"
    original, _ = repository_pair(source)
    store = FileArtifactStore(tmp_path / "evidence")
    generated = TestGenerator(store).seal_code(context(), CODE)
    environment = ExecutionEnvironment.model_validate(
        {"image": IMAGE, "policy": {"isolation": "reviewed_docker"}}
    )
    result = ReproductionExecutor(ScriptedBackend(store, ["backend_error"]), store).run(
        source, original, generated, environment
    )
    assert result.status is ReproductionStatus.ENVIRONMENT_ERROR
    assert result.executions == ()


def test_uncertain_container_cleanup_aborts_before_fixed_execution(tmp_path: Path) -> None:
    source = tmp_path / "repo"
    original, fixed = repository_pair(source)
    store = FileArtifactStore(tmp_path / "evidence")
    generated = TestGenerator(store).seal_code(context(), CODE)
    calls: list[PytestRunRequest] = []

    class UncertainBackend:
        def run_test(self, request: PytestRunRequest) -> ExecutionResult:
            calls.append(request)
            raise ExecutionBackendError(
                "cleanup cannot be confirmed",
                execution(137, Termination.INFRA_ERROR),
                stage="cleanup",
            )

    environment = ExecutionEnvironment.model_validate(
        {"image": IMAGE, "policy": {"isolation": "reviewed_docker"}}
    )
    with pytest.raises(ExecutionBackendError):
        DifferentialVerifier(ReproductionExecutor(UncertainBackend(), store), store).verify(
            source,
            original,
            fixed,
            generated,
            environment,
        )
    assert len(calls) == 1
    assert any(
        b"aborted_backend_failure" in path.read_bytes()
        for path in (store.root / "objects").iterdir()
    )
