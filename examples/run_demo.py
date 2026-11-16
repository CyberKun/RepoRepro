"""Run the CLI against an owned bug/fix fixture, then export and replay its Capsule."""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def run(directory: Path, image: str) -> None:
    from examples.create_reproduction_demo import create

    create(directory, image)

    def command(*arguments: str) -> subprocess.CompletedProcess[str]:
        print("$ repro " + " ".join(arguments), flush=True)
        result = subprocess.run(
            [sys.executable, "-m", "repo_repro", *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=180,
        )
        if result.returncode:
            print(result.stdout + result.stderr, file=sys.stderr)
            raise RuntimeError(f"command failed with exit {result.returncode}")
        return result

    def read(name: str) -> dict[str, Any]:
        value: dict[str, Any] = json.loads((directory / name).read_text(encoding="utf-8"))
        return value

    command(
        "generate",
        str(directory / "context.json"),
        "--test-file",
        str(directory / "test_repro.py"),
        "--artifacts",
        str(directory / "evidence"),
        "--output",
        str(directory / "generated.json"),
    )
    print("Sealed reviewed test: " + read("generated.json")["sha256"], flush=True)
    commits = read("commits.json")
    shared = [
        str(directory / "generated.json"),
        str(directory / "environment.json"),
        "--artifacts",
        str(directory / "evidence"),
    ]
    command(
        "reproduce",
        str(directory / "repository"),
        *shared,
        "--commit",
        commits["original"],
        "--output",
        str(directory / "reproduction.json"),
    )
    reproduction = read("reproduction.json")
    print(f"{reproduction['status']} ({len(reproduction['executions'])} attempts)", flush=True)
    command(
        "verify",
        str(directory / "repository"),
        commits["original"],
        commits["fixed"],
        *shared,
        "--output",
        str(directory / "verification.json"),
    )
    verification = read("verification.json")
    print(
        f"{verification['status']}: original={verification['original']['status']}, "
        f"fixed={verification['fixed']['status']}",
        flush=True,
    )
    exported = command(
        "capsule",
        "export",
        str(directory / "verification.json"),
        str(directory / "case.zip"),
        "--artifacts",
        str(directory / "evidence"),
    )
    manifest = json.loads(exported.stdout)
    print(f"Capsule {manifest['schema_version']}: {manifest['mode']}", flush=True)
    command("capsule", "validate", str(directory / "case.zip"))
    print("Capsule schema, catalog and checksums validated", flush=True)
    replay = command(
        "capsule",
        "replay",
        str(directory / "case.zip"),
        str(directory / "replay"),
        "--policy",
        str(directory / "policy.json"),
    )
    report = json.loads(replay.stdout)
    print(
        f"{report['status']}: behavior={report['behavior_match']}, "
        f"environment={report['environment_match']}",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--image", help="Reviewed, locally cached image ID containing pytest")
    selection.add_argument("--images", type=Path, help="JSON from prepare_pytest_image.py --json")
    parser.add_argument("--output", type=Path, default=Path(".repo-repro/demo"))
    arguments = parser.parse_args()
    image = arguments.image
    if arguments.images:
        image = json.loads(arguments.images.read_text(encoding="utf-8-sig"))["pytest_image"]
    run(arguments.output, image)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main()
