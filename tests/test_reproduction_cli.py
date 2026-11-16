"""Actual CLI generation/sealing and differential dispatch with a controlled backend."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repo_repro.cli import app
from repo_repro.reproduction import cli as commands
from repo_repro.storage.artifacts import FileArtifactStore
from tests.integration.test_reproduction import repository_pair
from tests.repro_support import CODE, context
from tests.test_differential import IMAGE, ScriptedBackend


def test_cli_seal_and_verify_work_without_a_model_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    source = tmp_path / "repo"
    original, fixed = repository_pair(source)
    input_path = tmp_path / "input.json"
    input_path.write_text(context().model_dump_json(), encoding="utf-8")
    code_path = tmp_path / "test.py"
    code_path.write_text(CODE, encoding="utf-8")
    generated = tmp_path / "generated.json"
    store_path = tmp_path / "evidence"
    sealed = runner.invoke(
        app,
        [
            "generate",
            str(input_path),
            "--test-file",
            str(code_path),
            "--output",
            str(generated),
            "--artifacts",
            str(store_path),
        ],
    )
    assert sealed.exit_code == 0, sealed.output
    environment = tmp_path / "environment.json"
    environment.write_text(
        json.dumps({"image": IMAGE, "policy": {"isolation": "reviewed_docker"}}), encoding="utf-8"
    )
    backend = ScriptedBackend(FileArtifactStore(store_path), ["target", "target", "pass", "pass"])
    monkeypatch.setattr(commands, "DockerSandboxRunner", lambda **kwargs: backend)
    result_path = tmp_path / "verification.json"
    verified = runner.invoke(
        app,
        [
            "verify",
            str(source),
            original,
            fixed,
            str(generated),
            str(environment),
            "--output",
            str(result_path),
            "--artifacts",
            str(store_path),
        ],
    )
    assert verified.exit_code == 0, verified.output
    assert json.loads(result_path.read_bytes())["status"] == "DIFFERENTIAL_VALIDATED"


def test_cli_requires_model_or_explicit_test_file(tmp_path: Path) -> None:
    input_path = tmp_path / "input.json"
    input_path.write_text(context().model_dump_json(), encoding="utf-8")
    result = CliRunner().invoke(
        app, ["generate", str(input_path), "--artifacts", str(tmp_path / "data")]
    )
    assert result.exit_code == 3
    assert "--model or --test-file" in result.output
