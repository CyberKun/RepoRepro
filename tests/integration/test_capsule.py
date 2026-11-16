"""A self-contained ZIP replays without the original repository or original artifact store."""

from pathlib import Path

import pytest
from tests.integration.test_reproduction import pytest_image as pytest_image
from tests.integration.test_reproduction import repository_pair
from tests.repro_support import CODE, context

from repo_repro.capsule.archive import extract_capsule, validate_capsule
from repo_repro.capsule.exporter import CapsuleExporter
from repo_repro.capsule.images import DockerImageArchive
from repo_repro.capsule.replay import CapsuleReplay
from repo_repro.reproduction.executor import ReproductionExecutor
from repo_repro.reproduction.generator import TestGenerator
from repo_repro.reproduction.models import DifferentialStatus, ExecutionEnvironment
from repo_repro.reproduction.planner import build_input
from repo_repro.sandbox.docker_runner import DockerSandboxRunner
from repo_repro.storage.artifacts import FileArtifactStore
from repo_repro.verification.differential import DifferentialVerifier

pytestmark = pytest.mark.docker_integration


def test_real_capsule_export_restore_and_offline_snapshot_replay(
    tmp_path: Path, pytest_image: str
) -> None:
    source = tmp_path / "source"
    original, fixed = repository_pair(source)
    store = FileArtifactStore(tmp_path / "original-evidence")
    data = build_input(
        context().issue,
        source,
        context().oracle,
        ref=original,
        source_paths=("calculator.py",),
        test_paths=("tests/test_calculator.py",),
    )
    generated = TestGenerator(store).seal_code(data, CODE)
    environment = ExecutionEnvironment.model_validate(
        {"image": pytest_image, "policy": {"isolation": "reviewed_docker"}}
    )
    backend = DockerSandboxRunner(image=pytest_image, artifacts=store)
    recorded = DifferentialVerifier(ReproductionExecutor(backend, store), store).verify(
        source, original, fixed, generated, environment
    )
    assert recorded.status is DifferentialStatus.DIFFERENTIAL_VALIDATED
    capsule = tmp_path / "case.zip"
    manifest = CapsuleExporter(store).export(recorded, capsule)
    assert validate_capsule(capsule) == manifest
    inspected = tmp_path / "unpacked"
    extract_capsule(capsule, inspected)
    assert manifest.image is not None
    DockerImageArchive().ensure(
        manifest.image.image_id, inspected / manifest.image.path, force_load=True
    )
    # Hide the inputs instead of deleting them: replay must not consult either path.
    assert source.resolve().is_relative_to(tmp_path.resolve())
    source.rename(tmp_path / "source-unavailable")
    assert store.root.resolve().is_relative_to(tmp_path.resolve())
    store.root.rename(tmp_path / "original-evidence-unavailable")
    result = CapsuleReplay().replay(
        capsule, tmp_path / "new-machine", local_policy=environment.policy, authorized=True
    )
    assert result.status == "MATCHED", result.reason
    assert result.replay_status == "DIFFERENTIAL_VALIDATED"
    assert result.behavior_match and result.environment_match
    assert (tmp_path / "new-machine/replay-report.json").is_file()
    assert (tmp_path / "new-machine/replay-report.md").is_file()
