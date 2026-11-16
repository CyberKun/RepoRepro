"""Build bounded, commit-bound generation inputs from read-only Git objects."""

from pathlib import Path

from repo_repro.analyzer.python_analyzer import PythonAnalyzer
from repo_repro.analyzer.repository import GitRepository
from repo_repro.core.context import IssueContext
from repo_repro.core.exceptions import InputError
from repo_repro.github.issue_parser import IssueParser
from repo_repro.reproduction.models import FailureOracle, ReproductionInput, SourceFile


def validate_input(context: ReproductionInput) -> None:
    if context.oracle.issue_quote not in context.issue.body:
        raise InputError("oracle must be anchored in the supplied Issue body")
    if context.issue.repository_url != context.repository.repository_url:
        raise InputError("Issue and repository identities differ")
    if "pytest" not in context.environment.test_frameworks:
        raise InputError("the first reproduction engine requires pytest")
    files = (*context.relevant_source_files, *context.existing_tests)
    if len({item.path for item in files}) != len(files):
        raise InputError("duplicate context files")
    if any(item.path not in context.repository.files for item in files):
        raise InputError("context file is absent from the analyzed Git tree")
    if sum(len(item.content.encode()) for item in files) > 262_144:
        raise InputError("source/test context exceeds the 256 KiB budget")


def build_input(
    issue: IssueContext,
    repository_path: Path,
    oracle: FailureOracle,
    *,
    ref: str = "HEAD",
    source_paths: tuple[str, ...] = (),
    test_paths: tuple[str, ...] = (),
) -> ReproductionInput:
    repository = PythonAnalyzer().analyze(repository_path, url=issue.repository_url, ref=ref)
    related = IssueParser().relate(issue, repository)
    sources = source_paths or tuple(
        item.value
        for item in related.related_code_files
        if item.value in repository.files and item.value not in repository.test_files
    )
    if not sources:
        sources = tuple(
            path
            for path in repository.files
            if path.endswith(".py")
            and path not in repository.test_files
            and Path(path).name not in {"setup.py", "conftest.py"}
        )[:5]
    tests = (
        test_paths
        or tuple(item.value for item in related.possible_test_files)
        or repository.test_files[:3]
    )
    with GitRepository().open(repository_path, ref=repository.current_commit) as view:

        def load(paths: tuple[str, ...]) -> tuple[SourceFile, ...]:
            return tuple(
                SourceFile(path=path, content=view.read(path).decode("utf-8")) for path in paths
            )

        context = ReproductionInput(
            issue=issue,
            repository=repository,
            environment=repository.environment,
            relevant_source_files=load(sources),
            existing_tests=load(tests),
            oracle=oracle,
        )
    validate_input(context)
    return context
