"""Create an owned Unicode normalization bug and an intentionally weak reproduction seed."""

import argparse
import json
from pathlib import Path

from git import Actor, Repo

from repo_repro.reproduction.models import ExecutionEnvironment, FailureOracle


def create(directory: Path, image: str) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    repository = directory / "repository"
    repository.mkdir()
    (repository / "tests").mkdir()
    for index, name in enumerate(("config", "math", "storage", "archive", "misc")):
        (repository / f"a{index}_{name}.py").write_text(
            "def identity(value):\n    return value\n", encoding="utf-8"
        )
    module = repository / "normalizer.py"
    original_code = "def normalize_username(value):\n    return value.strip().lower()\n"
    module.write_text(original_code, encoding="utf-8")
    (repository / "tests/test_normalizer.py").write_text(
        "from normalizer import normalize_username\n\n"
        "def test_ascii():\n    assert normalize_username(' Alice ') == 'alice'\n",
        encoding="utf-8",
    )
    (repository / "requirements.txt").write_text("pytest==9.2.0\n", encoding="utf-8")
    actor = Actor("Repo Repro demo", "demo@example.invalid")
    with Repo.init(repository, initial_branch="main") as repo:
        repo.index.add(
            [
                str(path.relative_to(repository))
                for path in repository.rglob("*")
                if path.is_file() and ".git" not in path.parts
            ]
        )
        original = repo.index.commit(
            "Unicode case folding bug", author=actor, committer=actor, skip_hooks=True
        ).hexsha
        module.write_text(original_code.replace(".lower()", ".casefold()"), encoding="utf-8")
        repo.index.add(["normalizer.py"])
        fixed = repo.index.commit(
            "use Unicode case folding", author=actor, committer=actor, skip_hooks=True
        ).hexsha
    payload = {
        "number": 1,
        "html_url": "https://github.com/example/normalizer/issues/1",
        "title": "Username normalization misses Unicode case folding",
        "body": "### Expected behavior\nUse Unicode case folding for user names; 'Straße' should "
        "become 'strasse'. Strip surrounding whitespace.\n### Actual behavior\n"
        "normalize_username('Straße') returns 'straße'. ASCII names work.\n"
        "### Reproduction steps\n1. Normalize a German user name containing ß.\n"
        "2. Compare it with its case-folded name.\n",
    }
    oracle = FailureOracle(
        exception_type="AssertionError",
        message_contains="Unicode case folding",
        issue_quote="Unicode case folding",
        behavior_contract=(
            "Strip surrounding whitespace and use Unicode case folding for all user names."
        ),
    )
    environment = ExecutionEnvironment.model_validate(
        {"image": image, "policy": {"isolation": "reviewed_docker"}}
    )
    for name, data in (
        ("issue.json", payload),
        ("commits.json", {"original": original, "fixed": fixed}),
        ("oracle.json", oracle.model_dump()),
        ("environment.json", environment.model_dump()),
        ("policy.json", environment.policy.model_dump()),
    ):
        (directory / name).write_text(json.dumps(data, indent=2), encoding="utf-8")
    (directory / "seed_test.py").write_text(
        "from normalizer import normalize_username\n\n"
        "def test_repro():\n"
        "    assert normalize_username('Alice') == 'alice', 'Unicode case folding'\n",
        encoding="utf-8",
    )
    print(json.dumps({"directory": str(directory), "original": original, "fixed": fixed}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    create(args.directory, args.image)


if __name__ == "__main__":
    main()
