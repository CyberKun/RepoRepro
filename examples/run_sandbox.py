"""Run an explicitly reviewed Git snapshot in an approved, locally cached image."""

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from repo_repro.core.exceptions import RepoReproError
from repo_repro.core.models import CommandSpec, SandboxPolicy, Termination
from repo_repro.sandbox.docker_runner import DockerSandboxRunner
from repo_repro.storage.artifacts import FileArtifactStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument(
        "--image", required=True, help="Immutable image ID or digest; never auto-pulled"
    )
    parser.add_argument(
        "--isolation", required=True, choices=["reviewed_docker", "disposable_vm_docker"]
    )
    parser.add_argument("--artifacts", type=Path, default=Path(".repo-repro/evidence"))
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument(
        "--install", action="store_true", help="Record a separate offline install attempt"
    )
    parser.add_argument("--command", nargs=argparse.REMAINDER, required=True)
    arguments = parser.parse_args()
    if not arguments.command:
        parser.error("--command requires a program and arguments")
    try:
        policy = SandboxPolicy.model_validate(
            {
                "isolation": arguments.isolation,
                "timeout_seconds": arguments.timeout,
            }
        )
        backend = DockerSandboxRunner(
            image=arguments.image,
            artifacts=FileArtifactStore(arguments.artifacts),
        )
        operation = backend.run_install if arguments.install else backend.run
        result = operation(
            arguments.repository,
            CommandSpec(argv=tuple(arguments.command)),
            resource_limits=policy,
        )
        print(result.model_dump_json(indent=2))
        return 0 if result.termination is Termination.EXITED and result.exit_code == 0 else 1
    except (RepoReproError, ValidationError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
