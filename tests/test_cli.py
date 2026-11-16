"""Exercise Typer behavior and the entry points produced by installation."""

import json
import os
import subprocess
import sys
import sysconfig
from importlib.metadata import version
from pathlib import Path

import pytest
from git import Repo
from typer.testing import CliRunner

from repo_repro import __version__
from repo_repro.cli import app

runner = CliRunner()


def test_help_exposes_version_command() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Usage:" in result.output
    assert "version" in result.output
    assert "Repo Repro" in result.output


def test_version_matches_installed_metadata() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert __version__ == version("repo-repro")
    assert result.output.strip() == f"Repo Repro {__version__}"


def test_unknown_command_is_a_usage_error() -> None:
    result = runner.invoke(app, ["command-that-does-not-exist"])

    assert result.exit_code == 2
    assert "No such command" in result.output


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [(("--help",), "Usage:"), (("version",), f"Repo Repro {__version__}")],
)
def test_installed_console_script_outside_repository(
    tmp_path: Path, arguments: tuple[str, ...], expected: str
) -> None:
    script = Path(sysconfig.get_path("scripts")) / ("repro.exe" if os.name == "nt" else "repro")

    result = subprocess.run(
        [str(script), *arguments],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert expected in result.stdout


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [(("--help",), "Usage:"), (("version",), f"Repo Repro {__version__}")],
)
def test_module_entry_point_outside_repository(
    tmp_path: Path, arguments: tuple[str, ...], expected: str
) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "repo_repro", *arguments],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert expected in result.stdout


def test_issue_command_accepts_local_json_without_network() -> None:
    fixture = Path(__file__).parent / "fixtures" / "issue.json"
    result = runner.invoke(app, ["issue", "--issue-json", str(fixture)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["source"] == "local_json"


def test_analyze_command_supports_an_entirely_offline_repository(tmp_path: Path) -> None:
    source = tmp_path / "owned-source"
    source.mkdir()
    (source / "main.py").write_text("raise RuntimeError('must not execute')", encoding="utf-8")
    with Repo.init(source, initial_branch="main") as repo:
        repo.index.add(["main.py"])
        repo.index.commit("owned fixture", skip_hooks=True)
    fixture = Path(__file__).parent / "fixtures" / "issue.json"
    output = tmp_path / "analysis.json"
    result = runner.invoke(
        app,
        [
            "analyze",
            "--issue-json",
            str(fixture),
            "--repository-path",
            str(source),
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    context = json.loads(output.read_text(encoding="utf-8"))
    assert context["repository"]["files"] == ["main.py"]
    assert context["repository"]["repository_identity_verified"] is False


def test_issue_command_requires_an_explicit_input() -> None:
    result = runner.invoke(app, ["issue"])
    assert result.exit_code == 2
