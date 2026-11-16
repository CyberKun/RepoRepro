"""Read-only analysis with synthetic manifests and real, self-owned Git objects."""

import json
import subprocess
from pathlib import Path

import pytest
from git import Repo

from repo_repro.analyzer.python_analyzer import PythonAnalyzer
from repo_repro.analyzer.repository import GitRepository, git_environment
from repo_repro.core.context import EvidenceOrigin, RepositoryInfo
from repo_repro.core.exceptions import InputError, RepositoryError
from repo_repro.github.issue_parser import IssueParser


class MemoryView:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted(self.files))

    def read(self, path: str) -> bytes:
        return self.files[path]


def inspect(files: dict[str, bytes]) -> RepositoryInfo:
    return PythonAnalyzer().inspect(
        MemoryView(files),
        url="https://github.com/example/project",
        commit="a" * 40,
        default_branch="main",
        ref="HEAD",
    )


def test_python_manifests_pytest_and_entry_points_are_detected() -> None:
    info = inspect(
        {
            "pyproject.toml": b"""[project]
requires-python = ">=3.11"
dependencies = ["requests"]
[project.optional-dependencies]
test = ["pytest>=8"]
[project.scripts]
example = "example.cli:main"
[tool.pytest.ini_options]
testpaths = ["tests"]
""",
            "requirements.txt": b"requests==2.32.0\n",
            "poetry.lock": b'[[package]]\nname = "pytest"\nversion = "8.3.0"\n',
            "src/example/__main__.py": b"raise RuntimeError('never run')",
            "tests/test_parser.py": b"import pytest\n",
            "src/parser.py": b"def parse(): return []",
        }
    )
    assert info.environment.python_requirements == (">=3.11",)
    assert set(info.environment.dependency_files) == {
        "pyproject.toml",
        "requirements.txt",
        "poetry.lock",
    }
    assert info.environment.test_frameworks == ("pytest",)
    assert info.test_directories == ("tests",)
    assert info.test_files == ("tests/test_parser.py",)
    assert "example = example.cli:main" in {entry.value for entry in info.entry_points}
    assert all(item.origin is EvidenceOrigin.STATIC_FACT for item in info.environment.evidence)


def test_poetry_python_and_pytest_groups_are_detected() -> None:
    info = inspect(
        {
            "pyproject.toml": b"""[tool.poetry.dependencies]
python = "^3.11"
[tool.poetry.group.dev.dependencies]
pytest = "^8"
"""
        }
    )
    assert info.environment.python_requirements == ("^3.11",)
    assert info.environment.test_frameworks == ("pytest",)


def test_python_version_files_and_setup_cfg_are_read_without_execution() -> None:
    info = inspect(
        {
            ".python-version": b"3.11.9\n",
            "runtime.txt": b"python-3.11.9\n",
            "setup.cfg": b"[options]\npython_requires = >=3.11\n[tool:pytest]\naddopts = -ra\n",
            "app.py": b"raise RuntimeError('not an entry point fact')",
        }
    )
    assert set(info.environment.python_requirements) == {"3.11.9", "python-3.11.9", ">=3.11"}
    assert info.entry_points[0].origin is EvidenceOrigin.HEURISTIC


def test_invalid_toml_does_not_invent_environment_details() -> None:
    info = inspect({"pyproject.toml": b"[broken", "main.py": b""})
    assert info.environment.python_requirements == ()
    assert info.warnings


def test_non_python_repository_is_rejected() -> None:
    with pytest.raises(RepositoryError, match="Python"):
        inspect({"main.go": b"package main"})


def test_issue_test_suggestions_are_heuristics_separate_from_facts() -> None:
    info = inspect({"src/parser.py": b"", "tests/test_parser.py": b""})
    parser = IssueParser()
    payload: object = json.loads((Path(__file__).parent / "fixtures" / "issue.json").read_bytes())
    context = parser.relate(parser.parse_payload(payload), info)
    assert context.repository == info
    assert context.possible_test_files[0].value == "tests/test_parser.py"
    assert context.possible_test_files[0].origin is EvidenceOrigin.HEURISTIC
    assert context.ai_inferences == ()


def test_real_git_analysis_reads_committed_tree_not_modified_working_files(tmp_path: Path) -> None:
    path = tmp_path / "owned-project"
    path.mkdir()
    marker = tmp_path / "must-not-exist"
    setup = path / "setup.py"
    setup.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        "setup(python_requires='>=3.11')\n",
        encoding="utf-8",
    )
    (path / "requirements.txt").write_text("pytest>=8\n", encoding="utf-8")
    (path / "tests").mkdir()
    (path / "tests" / "test_parser.py").write_text("import pytest\n", encoding="utf-8")
    with Repo.init(path, initial_branch="main") as repository:
        repository.index.add(["setup.py", "requirements.txt", "tests/test_parser.py"])
        commit = repository.index.commit("self-owned fixture", skip_hooks=True).hexsha
    setup.write_text("raise RuntimeError('dirty worktree')", encoding="utf-8")

    result = PythonAnalyzer().analyze(path, url="https://github.com/example/project")

    assert result.current_commit == commit
    assert result.environment.python_requirements == (">=3.11",)
    assert result.environment.test_frameworks == ("pytest",)
    assert not marker.exists()


def test_git_tree_read_limits_and_path_validation(tmp_path: Path) -> None:
    with Repo.init(tmp_path, initial_branch="main") as repository:
        (tmp_path / "main.py").write_bytes(b"x" * 20)
        repository.index.add(["main.py"])
        repository.index.commit("fixture", skip_hooks=True)
    with GitRepository(max_file_bytes=10).open(tmp_path) as view:
        with pytest.raises(RepositoryError, match="too large"):
            view.read("main.py")
        with pytest.raises(ValueError):
            view.read("../../secret")


def test_clone_is_bare_uses_safe_environment_and_never_runs_local_protocol(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(arguments)
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert "GITHUB_TOKEN" not in environment
        assert "GIT_SSH_COMMAND" not in environment
        return subprocess.CompletedProcess(arguments, 0, b"", b"")

    monkeypatch.setenv("GITHUB_TOKEN", "private")
    monkeypatch.setenv("GIT_SSH_COMMAND", "malicious")
    monkeypatch.setattr(subprocess, "run", fake_run)
    GitRepository().clone("https://github.com/example/project", tmp_path / "clone.git")
    assert "--bare" in calls[0]
    assert "--depth=1" in calls[0]
    assert "protocol.allow=never" in calls[0]
    assert "http.followRedirects=false" in calls[0]
    with pytest.raises(InputError):
        GitRepository().clone("ext::sh -c id", tmp_path / "bad")


def test_clone_timeout_is_a_typed_repository_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired("git", 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(RepositoryError, match="timed out"):
        GitRepository(timeout=1).clone("https://github.com/example/project", tmp_path / "clone")


def test_windows_systemroot_survives_environment_sanitization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYSTEMROOT", "C:/Windows")
    monkeypatch.setenv("GIT_SSH_COMMAND", "must-not-execute")
    environment = git_environment()
    assert any(
        key.upper() == "SYSTEMROOT" and value == "C:/Windows" for key, value in environment.items()
    )
    assert "GIT_SSH_COMMAND" not in environment
    assert environment["GIT_ALLOW_PROTOCOL"] == "https"


def test_missing_git_ref_is_a_typed_error(tmp_path: Path) -> None:
    with Repo.init(tmp_path, initial_branch="main") as repository:
        (tmp_path / "main.py").write_bytes(b"print('fixture')\n")
        repository.index.add(["main.py"])
        repository.index.commit("fixture", skip_hooks=True)
    with pytest.raises(RepositoryError):
        GitRepository().open(tmp_path, ref="not-a-real-branch")


def test_local_origin_configuration_alone_is_not_verified_identity(tmp_path: Path) -> None:
    with Repo.init(tmp_path, initial_branch="main") as repository:
        (tmp_path / "main.py").write_bytes(b"print('fixture')\n")
        repository.index.add(["main.py"])
        repository.index.commit("fixture", skip_hooks=True)
        repository.create_remote("origin", "https://github.com/example/project")
    information = PythonAnalyzer().analyze(tmp_path, url="https://github.com/example/project")
    assert information.origin_url == "https://github.com/example/project"
    assert information.repository_identity_verified is False
