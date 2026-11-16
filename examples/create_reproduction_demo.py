"""Create a self-owned two-commit bug/fix demo without executing its code on the host."""

import argparse
import json
from pathlib import Path

from git import Actor, Repo

from repo_repro.github.issue_parser import IssueParser
from repo_repro.reproduction.models import ExecutionEnvironment, FailureOracle
from repo_repro.reproduction.planner import build_input


def create(directory: Path, image: str) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    repository = directory / "repository"
    repository.mkdir()
    module = repository / "calculator.py"
    module.write_text(
        "def parse_value(value):\n    return -1 if value == '' else len(value)\n", encoding="utf-8"
    )
    (repository / "requirements.txt").write_text("pytest==9.2.0\n", encoding="utf-8")
    actor = Actor("Repo Repro demo", "demo@example.invalid")
    with Repo.init(repository, initial_branch="main") as git:
        git.index.add(["calculator.py", "requirements.txt"])
        original = git.index.commit(
            "original bug", author=actor, committer=actor, skip_hooks=True
        ).hexsha
        module.write_text("def parse_value(value):\n    return len(value)\n", encoding="utf-8")
        git.index.add(["calculator.py"])
        fixed = git.index.commit(
            "fix empty input", author=actor, committer=actor, skip_hooks=True
        ).hexsha
    payload = {
        "number": 1,
        "html_url": "https://github.com/example/calculator/issues/1",
        "title": "Empty input returns -1",
        "body": "### Expected behavior\nreturns zero\n### Actual behavior\nreturns -1",
    }
    (directory / "issue.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    issue = IssueParser().parse_payload(payload)
    oracle = FailureOracle(
        exception_type="AssertionError",
        message_contains="returns zero",
        issue_quote="returns zero",
        behavior_contract="Empty input returns zero",
    )
    data = build_input(issue, repository, oracle, ref=original, source_paths=("calculator.py",))
    (directory / "context.json").write_text(data.model_dump_json(indent=2), encoding="utf-8")
    (directory / "test_repro.py").write_text(
        "from calculator import parse_value\n\ndef test_repro():\n"
        "    assert parse_value('') == 0, 'returns zero'\n",
        encoding="utf-8",
    )
    environment = ExecutionEnvironment.model_validate(
        {"image": image, "policy": {"isolation": "reviewed_docker"}}
    )
    (directory / "environment.json").write_text(
        environment.model_dump_json(indent=2), encoding="utf-8"
    )
    (directory / "policy.json").write_text(
        environment.policy.model_dump_json(indent=2), encoding="utf-8"
    )
    (directory / "commits.json").write_text(
        json.dumps({"original": original, "fixed": fixed}, indent=2), encoding="utf-8"
    )
    print(json.dumps({"directory": str(directory), "original": original, "fixed": fixed}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--image", required=True)
    arguments = parser.parse_args()
    create(arguments.directory, arguments.image)


if __name__ == "__main__":
    main()
