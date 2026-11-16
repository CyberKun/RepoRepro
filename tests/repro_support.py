"""Synthetic evidence, never execution of a third-party repository on the host."""

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from repo_repro.core.context import EnvironmentInfo, RepositoryInfo
from repo_repro.core.models import (
    ArtifactRef,
    CommandSpec,
    ExecutionResult,
    RepositoryRevision,
    Termination,
)
from repo_repro.github.issue_parser import IssueParser
from repo_repro.reproduction.models import (
    NODE_ID,
    FailureOracle,
    PytestEvidence,
    PytestPhase,
    ReproductionInput,
    SourceFile,
    TestProposal,
)

CODE = (
    "from calculator import parse_value\n\ndef test_repro():\n"
    "    assert parse_value('') == 0, 'returns zero'\n"
)


def context() -> ReproductionInput:
    issue = IssueParser().parse_payload(
        {
            "html_url": "https://github.com/example/calculator/issues/1",
            "number": 1,
            "title": "Empty input returns -1",
            "body": "### Expected behavior\nreturns zero\n### Actual behavior\nreturns -1",
        }
    )
    environment = EnvironmentInfo(test_frameworks=("pytest",))
    return ReproductionInput(
        issue=issue,
        repository=RepositoryInfo(
            repository_url=issue.repository_url,
            default_branch="main",
            current_commit="a" * 40,
            analyzed_ref="HEAD",
            files=("calculator.py", "tests/test_calculator.py"),
            environment=environment,
        ),
        environment=environment,
        relevant_source_files=(
            SourceFile(path="calculator.py", content="def parse_value(value):\n    return -1\n"),
        ),
        existing_tests=(
            SourceFile(
                path="tests/test_calculator.py", content="def test_ok():\n    assert 1 == 1\n"
            ),
        ),
        oracle=FailureOracle(
            exception_type="AssertionError",
            message_contains="returns zero",
            issue_quote="returns zero",
            behavior_contract="Empty input returns zero",
        ),
    )


def proposal() -> TestProposal:
    return TestProposal.model_validate(
        {
            "understanding": "Empty input violates the expected return value.",
            "plan": "Follow tests/test_calculator.py; assert correct output for empty input.",
            "closest_test": "tests/test_calculator.py",
            "test_code": CODE,
            "evidence": [
                {"source": "issue.body", "quote": "returns zero"},
                {"source": "calculator.py", "quote": "return -1"},
            ],
        }
    )


def artifact(path: str = "evidence.bin") -> ArtifactRef:
    return ArtifactRef(path=path, sha256="b" * 64, size_bytes=0, media_type="application/json")


def execution(code: int = 1, termination: Termination = Termination.EXITED) -> ExecutionResult:
    return ExecutionResult(
        id=uuid4(),
        run_id=uuid4(),
        plan_id=uuid4(),
        phase="test",
        repetition=1,
        revision=RepositoryRevision(
            repository_url="local:fixture", commit="a" * 40, source=artifact()
        ),
        environment_id=uuid4(),
        command=CommandSpec(argv=("python", "-m", "pytest")),
        backend_snapshot=artifact(),
        exit_code=code,
        termination=termination,
        stdout=artifact(),
        stderr=artifact(),
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        duration_seconds=0.1,
        logs_complete=True,
    )


def report(
    *,
    passed: bool = False,
    exception_type: str = "AssertionError",
    message: str = "returns zero",
) -> PytestEvidence:
    phase_names: tuple[Literal["setup", "call", "teardown"], ...] = ("setup", "call", "teardown")
    phases = tuple(
        PytestPhase(
            node_id=NODE_ID,
            phase=phase,
            outcome="failed" if phase == "call" and not passed else "passed",
            was_xfail=False,
            exception_type=exception_type if phase == "call" and not passed else None,
            message=message if phase == "call" and not passed else None,
            traceback=None,
        )
        for phase in phase_names
    )
    return PytestEvidence(
        schema_version="1.0",
        pytest_version="9.2.0",
        collected=(NODE_ID,),
        collection_complete=True,
        collection_errors=(),
        phases=phases,
        session_complete=True,
        exit_code=0 if passed else 1,
    )
