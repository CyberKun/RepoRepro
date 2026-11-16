"""Minimal generation -> sandbox -> independent verdict loop, with no project edits."""

from pathlib import Path

from repo_repro.reproduction.executor import ReproductionExecutor
from repo_repro.reproduction.generator import TestGenerator
from repo_repro.reproduction.models import (
    ExecutionEnvironment,
    ReproductionInput,
    ReproductionResult,
)


class ReproductionEngine:
    def __init__(self, generator: TestGenerator, executor: ReproductionExecutor) -> None:
        self.generator = generator
        self.executor = executor

    def reproduce(
        self,
        context: ReproductionInput,
        repository_path: Path,
        environment: ExecutionEnvironment,
    ) -> ReproductionResult:
        generated = self.generator.generate(context)
        return self.executor.run(
            repository_path, context.repository.current_commit, generated, environment
        )
