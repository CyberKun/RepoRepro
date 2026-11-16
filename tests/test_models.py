"""Check validation boundaries and portable serialization of execution evidence."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from repo_repro.core.models import (
    ArtifactRef,
    CommandSpec,
    ExecutionResult,
    RepositoryRevision,
    SandboxPolicy,
    Termination,
)


def artifact(path: str = "logs/stdout.bin") -> ArtifactRef:
    return ArtifactRef(
        path=path, sha256="a" * 64, size_bytes=0, media_type="application/octet-stream"
    )


def execution_payload() -> dict[str, object]:
    started_at = datetime(2026, 11, 16, tzinfo=UTC)
    return {
        "id": uuid4(),
        "run_id": uuid4(),
        "plan_id": uuid4(),
        "phase": "test",
        "repetition": 1,
        "revision": RepositoryRevision(
            repository_url="https://github.com/example/project",
            commit="b" * 40,
            source=artifact("sources/bug.tar"),
        ),
        "environment_id": uuid4(),
        "command": CommandSpec(argv=("python", "-m", "pytest")),
        "backend_snapshot": artifact("execution/backend.json"),
        "exit_code": 1,
        "termination": Termination.EXITED,
        "stdout": artifact(),
        "stderr": artifact("logs/stderr.bin"),
        "started_at": started_at,
        "finished_at": started_at + timedelta(seconds=1),
        "duration_seconds": 1.0,
        "logs_complete": True,
        "pytest_report": artifact("execution/pytest-events.json"),
    }


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".",
        "../credentials",
        "logs/../credentials",
        "/etc/passwd",
        "C:/Users/secret",
        "C:\\Users\\secret",
        "\\\\server\\share",
        "logs//output",
        "logs/./output",
        "logs/output/",
        "logs/output:stream",
        "logs/output.",
        "logs/output ",
        "NUL",
        "logs/con.txt",
        "logs/COM¹.txt",
        "logs/out\x00put",
        "logs/out\x1bput",
    ],
)
def test_artifact_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValidationError, match="portable relative POSIX path"):
        artifact(path)


def test_artifact_accepts_portable_nested_path() -> None:
    reference = artifact("executions/test-1/stdout.bin")

    assert ArtifactRef.model_validate_json(reference.model_dump_json()) == reference


@pytest.mark.parametrize(
    "change",
    [{"sha256": "abc"}, {"sha256": "A" * 64}, {"size_bytes": -1}, {"unknown_field": True}],
)
def test_artifact_rejects_invalid_metadata(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ArtifactRef.model_validate(artifact().model_dump() | change)


@pytest.mark.parametrize("length", [40, 64])
def test_revision_accepts_complete_git_object_ids(length: int) -> None:
    revision = RepositoryRevision(
        repository_url="https://github.com/example/project",
        commit="b" * length,
        source=artifact("sources/bug.tar"),
    )

    assert revision.commit == "b" * length


def test_revision_rejects_moving_or_abbreviated_ref() -> None:
    with pytest.raises(ValidationError):
        RepositoryRevision(
            repository_url="https://github.com/example/project",
            commit="main",
            source=artifact("sources/bug.tar"),
        )


@pytest.mark.parametrize(
    "change",
    [
        {"argv": []},
        {"argv": [""]},
        {"argv": ["python", "\x00"]},
        {"cwd": "/etc"},
        {"cwd": "/workspace/../etc"},
        {"cwd": "/workspace//tests"},
        {"env": [("A", "1"), ("A", "2")]},
        {"env": [("", "value")]},
        {"env": [("A=B", "value")]},
        {"env": [("A", "\x00")]},
    ],
)
def test_command_rejects_invalid_process_fields(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CommandSpec.model_validate({"argv": ["python", "-m", "pytest"]} | change)


def test_command_preserves_empty_arguments_and_explicit_environment() -> None:
    command = CommandSpec(argv=("python", ""), cwd="/workspace/tests", env=(("TZ", "UTC"),))

    assert command.argv == ("python", "")
    assert command.env == (("TZ", "UTC"),)


def test_sandbox_requires_explicit_isolation_choice() -> None:
    with pytest.raises(ValidationError):
        SandboxPolicy.model_validate({})


def test_sandbox_defaults_disable_network_and_bound_resources() -> None:
    policy = SandboxPolicy(isolation="reviewed_docker")

    assert policy.network == "none"
    assert policy.cpus == 1.0
    assert policy.memory_bytes == 2_147_483_648
    assert policy.pids == 128
    assert policy.timeout_seconds == 120


@pytest.mark.parametrize(
    "change",
    [
        {"network": "bridge"},
        {"isolation": "unknown"},
        {"cpus": 0},
        {"cpus": float("inf")},
        {"cpus": float("nan")},
        {"memory_bytes": 0},
        {"memory_bytes": 8_589_934_593},
        {"pids": 0},
        {"pids": 513},
        {"timeout_seconds": 0},
        {"output_bytes_limit": 0},
        {"output_bytes_limit": 67_108_865},
        {"workspace_bytes_limit": 0},
        {"workspace_bytes_limit": 1_073_741_825},
    ],
)
def test_sandbox_rejects_invalid_policy_values(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SandboxPolicy.model_validate({"isolation": "reviewed_docker"} | change)


def test_execution_evidence_round_trips_as_json() -> None:
    result = ExecutionResult.model_validate(execution_payload())

    assert ExecutionResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "change",
    [
        {"exit_code": None},
        {"finished_at": datetime(2026, 11, 15, tzinfo=UTC)},
        {"started_at": datetime(2026, 11, 16)},
        {"duration_seconds": -1},
        {"duration_seconds": float("inf")},
        {"repetition": 0},
    ],
)
def test_execution_rejects_inconsistent_evidence(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ExecutionResult.model_validate(execution_payload() | change)


def test_timeout_can_be_recorded_without_an_exit_code_or_complete_report() -> None:
    result = ExecutionResult.model_validate(
        execution_payload()
        | {
            "termination": Termination.TIMEOUT,
            "exit_code": None,
            "logs_complete": False,
            "pytest_report": None,
        }
    )

    assert result.termination is Termination.TIMEOUT
    assert result.exit_code is None
    assert result.pytest_report is None


@pytest.mark.parametrize(("field_name", "new_value"), [("exit_code", 0), ("logs_complete", False)])
def test_execution_record_cannot_be_reassigned(field_name: str, new_value: int | bool) -> None:
    result = ExecutionResult.model_validate(execution_payload())

    with pytest.raises(ValidationError, match="frozen"):
        setattr(result, field_name, new_value)
