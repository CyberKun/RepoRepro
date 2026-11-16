"""Installed CLI end-to-end checks using only owned repositories and real Docker."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from git import Repo
from tests.integration.test_docker import assert_removed, commit_fixture, limits
from tests.integration.test_docker import backend as backend
from tests.integration.test_reproduction import pytest_image as pytest_image
from tests.integration.test_reproduction import repository_pair
from tests.repro_support import CODE, context

from repo_repro.core.models import CommandSpec, Termination
from repo_repro.reproduction.executor import ReproductionExecutor
from repo_repro.reproduction.generator import TestGenerator
from repo_repro.reproduction.models import ExecutionEnvironment, ReproductionStatus
from repo_repro.reproduction.planner import build_input
from repo_repro.sandbox.docker_runner import DockerSandboxRunner
from repo_repro.storage.artifacts import FileArtifactStore

pytestmark = pytest.mark.docker_integration


def cli(root: Path, *arguments: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "repo_repro", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        check=False,
        env=os.environ | {"PYTHONUTF8": "1", "NO_COLOR": "1"},
    )
    assert result.returncode == expected, result.stdout + result.stderr
    assert "Traceback (most recent call last)" not in result.stderr
    return result


def test_all_eight_modules_through_installed_cli(tmp_path: Path, pytest_image: str) -> None:
    source = tmp_path / "source"
    original, fixed = repository_pair(source)
    issue = context().issue
    issue_path = tmp_path / "issue.json"
    issue_path.write_text(
        json.dumps(
            {
                "number": issue.number,
                "html_url": issue.issue_url,
                "title": issue.title,
                "body": issue.body,
            }
        ),
        encoding="utf-8",
    )
    parsed = cli(tmp_path, "issue", "--issue-json", str(issue_path))
    assert json.loads(parsed.stdout)["body_trusted"] is False
    analysis = tmp_path / "analysis.json"
    cli(
        tmp_path,
        "analyze",
        "--issue-json",
        str(issue_path),
        "--repository-path",
        str(source),
        "--ref",
        original,
        "--output",
        str(analysis),
    )
    assert json.loads(analysis.read_bytes())["repository"]["current_commit"] == original
    data = build_input(
        issue,
        source,
        context().oracle,
        ref=original,
        source_paths=("calculator.py",),
        test_paths=("tests/test_calculator.py",),
    )
    input_path, code, generated = (
        tmp_path / name for name in ("input.json", "test.txt", "test.json")
    )
    input_path.write_text(data.model_dump_json(), encoding="utf-8")
    code.write_text(CODE, encoding="utf-8")
    evidence, environment = tmp_path / "evidence", tmp_path / "environment.json"
    environment.write_text(
        json.dumps({"image": pytest_image, "policy": {"isolation": "reviewed_docker"}}),
        encoding="utf-8",
    )
    cli(
        tmp_path,
        "generate",
        str(input_path),
        "--test-file",
        str(code),
        "--output",
        str(generated),
        "--artifacts",
        str(evidence),
    )
    reproduction = tmp_path / "reproduction.json"
    cli(
        tmp_path,
        "reproduce",
        str(source),
        str(generated),
        str(environment),
        "--commit",
        original,
        "--output",
        str(reproduction),
        "--artifacts",
        str(evidence),
    )
    observed = json.loads(reproduction.read_bytes())
    assert observed["status"] == "REPRODUCED" and len(observed["executions"]) == 2
    verification = tmp_path / "verification.json"
    cli(
        tmp_path,
        "verify",
        str(source),
        original,
        fixed,
        str(generated),
        str(environment),
        "--output",
        str(verification),
        "--artifacts",
        str(evidence),
    )
    verified = json.loads(verification.read_bytes())
    assert verified["status"] == "DIFFERENTIAL_VALIDATED"
    capsule = tmp_path / "case.zip"
    exported = cli(
        tmp_path, "capsule", "export", str(verification), str(capsule), "--artifacts", str(evidence)
    )
    manifest = json.loads(exported.stdout)
    assert manifest["mode"] == "offline"
    for command in ("validate", "inspect"):
        assert json.loads(cli(tmp_path, "capsule", command, str(capsule)).stdout) == manifest
    cli(tmp_path, "capsule", "unpack", str(capsule), str(tmp_path / "unpacked"))
    with Repo(source) as repo:
        assert repo.head.commit.hexsha == fixed and not repo.is_dirty(untracked_files=True)
    source.rename(tmp_path / "source-unavailable")
    evidence.rename(tmp_path / "evidence-unavailable")
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"isolation": "reviewed_docker"}), encoding="utf-8")
    replay = cli(
        tmp_path,
        "capsule",
        "replay",
        str(capsule),
        str(tmp_path / "replayed"),
        "--policy",
        str(policy),
    )
    outcome = json.loads(replay.stdout)
    assert outcome["status"] == "MATCHED"
    assert outcome["replay_status"] == "DIFFERENTIAL_VALIDATED"
    assert outcome["behavior_match"] and outcome["environment_match"]
    assert (tmp_path / "replayed/replay-report.json").is_file()


def test_real_empty_logs_preserve_a_successful_exit(
    tmp_path: Path, backend: DockerSandboxRunner
) -> None:
    source = commit_fixture(tmp_path / "source", "pass\n")
    result = backend.run(source, CommandSpec(argv=("python", "probe.py")), resource_limits=limits())
    assert result.termination is Termination.EXITED and result.exit_code == 0
    assert result.logs_complete and backend.read_output(result) == ("", "")
    assert result.stdout.size_bytes == result.stderr.size_bytes == 0
    assert_removed(backend, backend.artifacts.read_bytes(result.backend_snapshot))


def test_real_sigkill_is_recorded_and_cleaned(tmp_path: Path, backend: DockerSandboxRunner) -> None:
    source = commit_fixture(
        tmp_path / "source", "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n"
    )
    result = backend.run(source, CommandSpec(argv=("python", "probe.py")), resource_limits=limits())
    assert result.exit_code == 137
    assert_removed(backend, backend.artifacts.read_bytes(result.backend_snapshot))


def test_real_missing_project_dependency_is_not_a_reproduced_bug(
    tmp_path: Path, pytest_image: str
) -> None:
    source = tmp_path / "source"
    _, fixed = repository_pair(source)
    with Repo(source) as repo:
        (source / "calculator.py").write_text(
            "import missing_qa_fixture_dependency\n", encoding="utf-8"
        )
        repo.index.add(["calculator.py"])
        broken = repo.index.commit("missing dependency fixture", skip_hooks=True).hexsha
        assert broken != fixed
    store = FileArtifactStore(tmp_path / "evidence")
    generated = TestGenerator(store).seal_code(context(), CODE)
    backend = DockerSandboxRunner(image=pytest_image, artifacts=store)
    environment = ExecutionEnvironment.model_validate(
        {"image": pytest_image, "policy": {"isolation": "reviewed_docker"}}
    )
    result = ReproductionExecutor(backend, store).run(source, broken, generated, environment)
    assert result.status is ReproductionStatus.ENVIRONMENT_ERROR
    assert len(result.executions) == 2
    # A fully qualified missing target is pytest's USAGE_ERROR (4), with collection evidence.
    assert all(item.exit_code == 4 for item in result.executions)
    for item in result.executions:
        assert item.pytest_report is not None
        evidence = json.loads(store.read_bytes(item.pytest_report))
        assert evidence["collection_errors"] and evidence["collected"] == []
    assert all(
        "missing_qa_fixture_dependency" in backend.read_output(item)[0]
        for item in result.executions
    )
