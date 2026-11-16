"""Execution backend port shared by Docker and future stronger isolation backends."""

from pathlib import Path
from typing import Protocol

from repo_repro.core.models import CommandSpec, ExecutionResult, SandboxPolicy
from repo_repro.reproduction.models import PytestRunRequest, PytestSnapshotRequest


class PytestBackend(Protocol):
    def run_test(self, request: PytestRunRequest) -> ExecutionResult: ...


class PytestSnapshotBackend(Protocol):
    def run_snapshot(self, request: PytestSnapshotRequest) -> ExecutionResult: ...


class SandboxBackend(Protocol):
    def run(
        self,
        repository_path: Path,
        command: CommandSpec,
        *,
        timeout: int | None = None,
        resource_limits: SandboxPolicy,
    ) -> ExecutionResult: ...

    def run_install(
        self,
        repository_path: Path,
        command: CommandSpec,
        *,
        timeout: int | None = None,
        resource_limits: SandboxPolicy,
    ) -> ExecutionResult: ...
