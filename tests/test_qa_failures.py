"""Failure/security matrices with real Git objects and controlled external failures."""

import io
import json
import tarfile
from pathlib import Path

import pytest
from git import Repo
from typer.testing import CliRunner

from repo_repro.analyzer.python_analyzer import PythonAnalyzer
from repo_repro.analyzer.repository import GitRepository
from repo_repro.capsule.archive import extract_capsule, validate_capsule
from repo_repro.capsule.schema import CapsuleError, json_object
from repo_repro.capsule.security import SecretRedactor
from repo_repro.cli import app
from repo_repro.core.exceptions import InputError, IssueFetchError, PolicyViolation, RepositoryError
from repo_repro.core.models import CommandSpec, SandboxPolicy, Termination
from repo_repro.core.source_archives import source_files
from repo_repro.github.client import GitHubClient, HTTPResponse
from repo_repro.github.issue_parser import IssueParser
from repo_repro.reproduction.models import ReproductionStatus
from repo_repro.sandbox.docker_runner import DockerCLI, DockerSandboxRunner
from repo_repro.storage.artifacts import FileArtifactStore
from repo_repro.verification.verifier import assess_execution
from tests.repro_support import context, execution, report
from tests.test_github import FakeHTTP
from tests.test_sandbox import IMAGE, FakeDocker


def repository(path: Path, filename: str = "main.py") -> Path:
    path.mkdir()
    (path / filename).write_text("# self-owned fixture\n", encoding="utf-8")
    with Repo.init(path, initial_branch="main") as repo:
        repo.index.add([filename])
        repo.index.commit("QA fixture", skip_hooks=True)
    return path


@pytest.mark.parametrize("kind", ["missing", "ordinary_directory", "empty_git"])
def test_unusable_repository_is_a_typed_error(tmp_path: Path, kind: str) -> None:
    path = tmp_path / kind
    if kind == "ordinary_directory":
        path.mkdir()
    elif kind == "empty_git":
        Repo.init(path).close()
    with pytest.raises(RepositoryError):
        GitRepository().open(path)


@pytest.mark.parametrize("filename", ["main.go", "main.rs", "index.js", "README.md"])
def test_unsupported_languages_have_controlled_cli_errors(tmp_path: Path, filename: str) -> None:
    source = repository(tmp_path / "source", filename)
    result = CliRunner().invoke(
        app,
        [
            "analyze",
            "--issue-json",
            str(Path(__file__).parent / "fixtures/issue.json"),
            "--repository-path",
            str(source),
        ],
    )
    assert result.exit_code == 3
    assert "no supported Python project" in result.output
    assert not isinstance(result.exception, RepositoryError)


def test_missing_git_executable_has_no_partial_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("repo_repro.analyzer.repository.shutil.which", lambda _: None)
    target = tmp_path / "clone"
    with pytest.raises(RepositoryError, match="Git is not installed"):
        GitRepository().clone("https://github.com/o/r", target)
    assert not target.exists()


def test_missing_remote_repository_does_not_leak_git_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    def missing(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            ["git"], 128, b"", b"private-token: repository not found"
        )

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(RepositoryError, match="could not fetch") as failure:
        GitRepository().clone("https://github.com/o/missing", tmp_path / "clone")
    assert "private-token" not in str(failure.value)


@pytest.mark.parametrize("status", [401, 403, 404, 410, 503])
def test_inaccessible_issue_never_uses_error_body_as_context(status: int) -> None:
    transport = FakeHTTP([HTTPResponse(status, {}, b'{"message":"private-api-detail"}')])
    with pytest.raises(IssueFetchError) as failure:
        IssueParser(GitHubClient(transport=transport, retries=0)).parse(
            "https://github.com/o/r/issues/1"
        )
    assert failure.value.status == status
    assert "private-api-detail" not in str(failure.value)


@pytest.mark.parametrize("content", [b"", b"not JSON", b"[]", b'{"body": null}', b"[" * 3000])
def test_bad_issue_json_is_rejected(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "issue.json"
    path.write_bytes(content)
    with pytest.raises(InputError):
        IssueParser().parse(issue_json=path)


@pytest.mark.parametrize("content", [b"", b"PK", b"PK\x03\x04bad", b"not a zip"])
def test_corrupt_zip_never_publishes_an_extraction(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "broken.zip"
    path.write_bytes(content)
    with pytest.raises(CapsuleError):
        validate_capsule(path)
    destination = tmp_path / "unpacked"
    with pytest.raises(CapsuleError):
        extract_capsule(path, destination)
    assert not destination.exists()


@pytest.mark.parametrize("content", [b'{"a":1,"a":2}', b'{"a":NaN}', b"[" * 3000])
def test_ambiguous_capsule_metadata_is_rejected(content: bytes) -> None:
    with pytest.raises(CapsuleError):
        json_object(content)


@pytest.mark.parametrize("name", ["../escape", "/escape", "C:/escape", "a\\b", "a/../b", "NUL"])
def test_source_tar_traversal_is_rejected_without_extraction(name: str) -> None:
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w") as archive:
        item = tarfile.TarInfo(name)
        item.size = 1
        archive.addfile(item, io.BytesIO(b"x"))
    with pytest.raises((PolicyViolation, ValueError)):
        source_files(content.getvalue())


@pytest.mark.parametrize("content", [None, b"", b"{}", b"invalid", b"[" * 3000])
def test_empty_or_invalid_evidence_never_counts_as_a_bug(content: bytes | None) -> None:
    outcome = assess_execution(execution(), context().oracle, content)
    assert outcome.status is ReproductionStatus.INCONCLUSIVE


@pytest.mark.parametrize("code", [2, 3, 4, 5, 17, 127, 137, 139])
def test_abnormal_test_exit_cannot_match_even_a_valid_failure_report(code: int) -> None:
    outcome = assess_execution(
        execution(code), context().oracle, report().model_dump_json().encode()
    )
    assert outcome.status is ReproductionStatus.ENVIRONMENT_ERROR


@pytest.mark.parametrize("failure", ["missing_executable", "daemon_unavailable", "empty_json"])
def test_docker_preflight_failure_persists_evidence(tmp_path: Path, failure: str) -> None:
    from repo_repro.sandbox.process import ProcessOutput

    policy = SandboxPolicy(isolation="reviewed_docker")
    source = repository(tmp_path / "source")

    class UnavailableDocker(FakeDocker):
        def run(self, arguments: object, *, timeout: float = 15) -> ProcessOutput:
            return ProcessOutput(1 if failure == "daemon_unavailable" else 0, b"", b"")

    transport = (
        DockerCLI(executable=str(tmp_path / "missing-docker.exe"))
        if failure == "missing_executable"
        else UnavailableDocker(policy)
    )
    store = FileArtifactStore(tmp_path / "evidence")
    result = DockerSandboxRunner(image=IMAGE, artifacts=store, transport=transport).run(
        source, CommandSpec(argv=("python", "main.py")), resource_limits=policy
    )
    assert result.termination is Termination.INFRA_ERROR
    assert not result.resource_usage or not result.resource_usage.cgroup_verified
    assert json.loads(store.read_bytes(result.backend_snapshot))["cleanup_confirmed"] is True


@pytest.mark.parametrize(
    "raw",
    [
        "password=opaque-qa-secret",
        "Authorization: Bearer opaque-qa-secret",
        "https://username:opaque-qa-secret@example.invalid/path",
        'client_secret="opaque-qa-secret"',
        "-----BEGIN OPENSSH PRIVATE KEY-----\nopaque-qa-secret\n-----END OPENSSH PRIVATE KEY-----",
    ],
)
def test_share_time_redaction_removes_credentials(raw: str) -> None:
    redactor = SecretRedactor(include_environment=False)
    assert "opaque-qa-secret" not in redactor.text(raw)
    with pytest.raises(CapsuleError):
        redactor.critical(raw.encode())


def test_local_repository_read_never_executes_malicious_setup(tmp_path: Path) -> None:
    source = repository(tmp_path / "source", "setup.py")
    marker = tmp_path / "executed"
    with Repo(source) as repo:
        (source / "setup.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8"
        )
        repo.index.add(["setup.py"])
        repo.index.commit("inert hostile fixture", skip_hooks=True)
    assert PythonAnalyzer().analyze(source, url="https://github.com/o/r").files == ("setup.py",)
    assert not marker.exists()
