"""A controlled model plus real containers protects the complete feedback/verification loop."""

from pathlib import Path

import pytest
from tests.integration.test_reproduction import pytest_image as pytest_image
from tests.integration.test_reproduction import repository_pair
from tests.repro_support import context, proposal
from tests.test_ai_investigation import SEED, SequenceProvider, plan

from repo_repro.reproduction.executor import ReproductionExecutor
from repo_repro.reproduction.investigator import AIInvestigator
from repo_repro.reproduction.models import (
    DifferentialStatus,
    ExecutionEnvironment,
    ReproductionStatus,
)
from repo_repro.sandbox.docker_runner import DockerSandboxRunner
from repo_repro.storage.artifacts import FileArtifactStore

pytestmark = pytest.mark.docker_integration


def test_real_execution_feedback_revises_seed_before_frozen_verification(
    tmp_path: Path,
    pytest_image: str,
) -> None:
    source = tmp_path / "source"
    original, fixed = repository_pair(source)
    store = FileArtifactStore(tmp_path / "evidence")
    provider = SequenceProvider(store, [plan().model_dump_json(), proposal().model_dump_json()])
    backend = DockerSandboxRunner(image=pytest_image, artifacts=store)
    environment = ExecutionEnvironment.model_validate(
        {"image": pytest_image, "policy": {"isolation": "reviewed_docker"}},
    )
    session = AIInvestigator(provider, ReproductionExecutor(backend, store), store).run(
        context().issue,
        source,
        context().oracle,
        environment,
        ref=original,
        initial_code=SEED,
        fixed_commit=fixed,
    )
    assert session.model_calls == 2 and session.stop_reason == "reproduced"
    assert len(session.attempts) == 2 and session.attempts[0].result is not None
    assert session.attempts[0].result.status is ReproductionStatus.NOT_REPRODUCED
    assert session.attempts[1].result is not None
    assert session.attempts[1].result.status is ReproductionStatus.REPRODUCED
    assert session.verification is not None
    assert session.verification.status is DifferentialStatus.DIFFERENTIAL_VALIDATED
    feedback = provider.requests[1]["user"]
    assert isinstance(feedback, dict)
    assert len(feedback["feedback"]["observations"]) == 2
    assert all(item["exit_code"] == 0 for item in feedback["feedback"]["observations"])
