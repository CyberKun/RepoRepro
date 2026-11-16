"""Explicit generation/run/verification commands; the model cannot select these options."""

import os
from pathlib import Path

import typer
from pydantic import SecretStr, ValidationError

from repo_repro.ai.openai_compatible import OpenAICompatibleProvider, OpenAIConfig
from repo_repro.core.exceptions import InputError, RepoReproError
from repo_repro.core.models import Record
from repo_repro.reproduction.executor import ReproductionExecutor
from repo_repro.reproduction.generator import TestGenerator
from repo_repro.reproduction.models import (
    DifferentialStatus,
    ExecutionEnvironment,
    GeneratedTest,
    ReproductionInput,
    ReproductionStatus,
)
from repo_repro.sandbox.docker_runner import DockerSandboxRunner
from repo_repro.storage.artifacts import FileArtifactStore
from repo_repro.verification.differential import DifferentialVerifier


def read_json(path: Path) -> bytes:
    with path.open("rb") as stream:
        content = stream.read(524_289)
    if len(content) > 524_288:
        raise InputError("input exceeds 512 KiB")
    return content


def write_result(record: Record, output: Path | None) -> None:
    text = record.model_dump_json(indent=2)
    if output:
        output.write_text(text + "\n", encoding="utf-8")
        typer.echo(f"Saved to {output}")
    else:
        typer.echo(text)


def failure(error: Exception) -> None:
    message = (
        str(error)
        if isinstance(error, RepoReproError)
        else f"Invalid input or I/O ({type(error).__name__})"
    )
    typer.echo(message, err=True)
    raise typer.Exit(3) from None


def generate(
    context: Path,
    output: Path | None = None,
    artifacts: Path = Path(".repo-repro/evidence"),
    model: str | None = None,
    base_url: str = "https://api.openai.com/v1",
    local_http: bool = False,
    legacy_max_tokens: bool = False,
    test_file: Path | None = None,
) -> None:
    """Generate a candidate, or seal an explicit user test without AI."""
    try:
        data = ReproductionInput.model_validate_json(read_json(context))
        store = FileArtifactStore(artifacts)
        if test_file:
            candidate = TestGenerator(store).seal_code(data, read_json(test_file).decode("utf-8"))
        else:
            if model is None:
                raise InputError("provide --model or --test-file")
            key = os.environ.get("OPENAI_API_KEY")
            provider = OpenAICompatibleProvider(
                OpenAIConfig(
                    model=model,
                    base_url=base_url,
                    api_key=SecretStr(key) if key else None,
                    allow_local_http=local_http,
                    token_parameter="max_tokens" if legacy_max_tokens else "max_completion_tokens",
                ),
                store,
            )
            candidate = TestGenerator(store, provider).generate(data)
        write_result(candidate, output)
    except (RepoReproError, ValidationError, ValueError, OSError) as error:
        failure(error)


def reproduce(
    repository: Path,
    test: Path,
    environment: Path,
    commit: str = "HEAD",
    output: Path | None = None,
    artifacts: Path = Path(".repo-repro/evidence"),
) -> None:
    """Run a sealed test repeatedly in fresh sandbox containers."""
    try:
        generated = GeneratedTest.model_validate_json(read_json(test))
        settings = ExecutionEnvironment.model_validate_json(read_json(environment))
        store = FileArtifactStore(artifacts)
        backend = DockerSandboxRunner(image=settings.image, artifacts=store)
        result = ReproductionExecutor(backend, store).run(repository, commit, generated, settings)
        write_result(result, output)
        if result.status is not ReproductionStatus.REPRODUCED:
            raise typer.Exit(1)
    except (RepoReproError, ValidationError, ValueError, OSError) as error:
        failure(error)


def verify(
    repository: Path,
    original_commit: str,
    fixed_commit: str,
    test: Path,
    environment: Path,
    output: Path | None = None,
    artifacts: Path = Path(".repo-repro/evidence"),
) -> None:
    """Compare commits using the exact same test and runtime."""
    try:
        generated = GeneratedTest.model_validate_json(read_json(test))
        settings = ExecutionEnvironment.model_validate_json(read_json(environment))
        store = FileArtifactStore(artifacts)
        backend = DockerSandboxRunner(image=settings.image, artifacts=store)
        result = DifferentialVerifier(ReproductionExecutor(backend, store), store).verify(
            repository,
            original_commit,
            fixed_commit,
            generated,
            settings,
        )
        write_result(result, output)
        if result.status is not DifferentialStatus.DIFFERENTIAL_VALIDATED:
            raise typer.Exit(1)
    except (RepoReproError, ValidationError, ValueError, OSError) as error:
        failure(error)


def register(app: typer.Typer) -> None:
    app.command("generate")(generate)
    app.command("reproduce")(reproduce)
    app.command("verify")(verify)
