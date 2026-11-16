"""Real pytest loops and differential verification on a self-owned two-commit repository."""

import json
import os
from pathlib import Path

import pytest
from git import Repo
from pydantic import SecretStr
from tests.repro_support import CODE, context, proposal
from tests.test_ai_provider import SECRET, FakeTransport

from repo_repro.ai.openai_compatible import OpenAICompatibleProvider, OpenAIConfig
from repo_repro.core.models import SandboxPolicy
from repo_repro.reproduction.engine import ReproductionEngine
from repo_repro.reproduction.executor import ReproductionExecutor
from repo_repro.reproduction.generator import TestGenerator
from repo_repro.reproduction.models import (
    DifferentialStatus,
    ExecutionEnvironment,
    ReproductionStatus,
)
from repo_repro.reproduction.planner import build_input
from repo_repro.sandbox.docker_runner import DockerSandboxRunner
from repo_repro.storage.artifacts import FileArtifactStore
from repo_repro.verification.differential import DifferentialVerifier

pytestmark = pytest.mark.docker_integration


@pytest.fixture
def pytest_image() -> str:
    value = os.environ.get("REPRO_PYTEST_IMAGE")
    if not value:
        if os.environ.get("REPRO_REQUIRE_ENGINE") == "1":
            pytest.fail("REPRO_PYTEST_IMAGE is required")
        pytest.skip("set REPRO_PYTEST_IMAGE to an approved image containing pytest")
    return value


def repository_pair(path: Path) -> tuple[str, str]:
    path.mkdir()
    (path / "tests").mkdir()
    (path / "requirements.txt").write_text("pytest==9.2.0\n", encoding="utf-8")
    (path / "tests/test_calculator.py").write_text(
        "from calculator import parse_value\ndef test_non_empty():\n"
        "    assert parse_value('abc') == 3\n",
        encoding="utf-8",
    )
    module = path / "calculator.py"
    module.write_text(
        "def parse_value(value):\n    return -1 if value == '' else len(value)\n", encoding="utf-8"
    )
    with Repo.init(path, initial_branch="main") as repo:
        repo.index.add(["calculator.py", "requirements.txt", "tests/test_calculator.py"])
        original = repo.index.commit("original bug", skip_hooks=True).hexsha
        module.write_text("def parse_value(value):\n    return len(value)\n", encoding="utf-8")
        repo.index.add(["calculator.py"])
        fixed = repo.index.commit("fix empty input", skip_hooks=True).hexsha
    return original, fixed


def test_real_ai_loop_uses_mock_api_and_real_sandbox(
    tmp_path: Path,
    pytest_image: str,
) -> None:
    source = tmp_path / "repo"
    original, fixed = repository_pair(source)
    data = build_input(
        context().issue,
        source,
        context().oracle,
        ref=original,
        source_paths=("calculator.py",),
        test_paths=("tests/test_calculator.py",),
    )
    artifacts = FileArtifactStore(tmp_path / "evidence")
    provider = OpenAICompatibleProvider(
        OpenAIConfig(model="fixture", api_key=SecretStr(SECRET)),
        artifacts,
        transport=FakeTransport(proposal().model_dump_json()),
    )
    backend = DockerSandboxRunner(image=pytest_image, artifacts=artifacts)
    environment = ExecutionEnvironment(
        image=pytest_image, policy=SandboxPolicy(isolation="reviewed_docker")
    )
    engine = ReproductionEngine(
        TestGenerator(artifacts, provider), ReproductionExecutor(backend, artifacts)
    )
    result = engine.reproduce(data, source, environment)
    assert result.status is ReproductionStatus.REPRODUCED, result.reason
    assert len(result.executions) == 2 and not result.flaky
    assert all(item.pytest_report for item in result.executions)
    with Repo(source) as repo:
        assert repo.head.commit.hexsha == fixed
        assert not repo.is_dirty(untracked_files=True)
    assert not (source / "repo_repro_generated").exists()
    assert all(
        SECRET.encode() not in path.read_bytes() for path in (artifacts.root / "objects").iterdir()
    )


def test_real_differential_original_fails_fixed_passes(
    tmp_path: Path,
    pytest_image: str,
) -> None:
    source = tmp_path / "repo"
    original, fixed = repository_pair(source)
    data = build_input(
        context().issue,
        source,
        context().oracle,
        ref=original,
        source_paths=("calculator.py",),
        test_paths=("tests/test_calculator.py",),
    )
    artifacts = FileArtifactStore(tmp_path / "evidence")
    generated = TestGenerator(artifacts).seal_code(data, CODE)
    backend = DockerSandboxRunner(image=pytest_image, artifacts=artifacts)
    environment = ExecutionEnvironment.model_validate(
        {"image": pytest_image, "policy": {"isolation": "reviewed_docker"}}
    )
    result = DifferentialVerifier(ReproductionExecutor(backend, artifacts), artifacts).verify(
        source,
        original,
        fixed,
        generated,
        environment,
    )
    assert result.status is DifferentialStatus.DIFFERENTIAL_VALIDATED, result.reason
    assert result.original.status is ReproductionStatus.REPRODUCED
    assert result.fixed.status is ReproductionStatus.NOT_REPRODUCED
    assert len(set(result.environment_fingerprints)) == 1
    assert result.original_commit == original and result.fixed_commit == fixed
    executions = result.original.executions + result.fixed.executions
    assert len({item.id for item in executions}) == 4
    assert all(item.pytest_report for item in executions)
    assert {item.generated_test.sha256 for item in (result.original, result.fixed)} == {
        generated.sha256
    }
    for item in executions:
        metadata = json.loads(artifacts.read_bytes(item.backend_snapshot))
        assert metadata["cleanup_confirmed"] is True
    with Repo(source) as repo:
        assert repo.head.commit.hexsha == fixed and not repo.is_dirty(untracked_files=True)


def test_real_missing_pytest_is_an_environment_error(tmp_path: Path, pytest_image: str) -> None:
    base = os.environ.get("REPRO_DOCKER_TEST_IMAGE")
    if not base:
        pytest.skip("base Python image required for missing-framework scenario")
    source = tmp_path / "repo"
    original, _ = repository_pair(source)
    artifacts = FileArtifactStore(tmp_path / "evidence")
    generated = TestGenerator(artifacts).seal_code(context(), CODE)
    backend = DockerSandboxRunner(image=base, artifacts=artifacts)
    environment = ExecutionEnvironment.model_validate(
        {"image": base, "policy": {"isolation": "reviewed_docker"}}
    )
    result = ReproductionExecutor(backend, artifacts).run(source, original, generated, environment)
    assert result.status is ReproductionStatus.ENVIRONMENT_ERROR
