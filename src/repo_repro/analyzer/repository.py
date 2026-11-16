"""Bounded bare cloning and Git blob access without checkout or target execution."""

import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from repo_repro.core.exceptions import InputError, RepositoryError
from repo_repro.core.models import ArtifactRef
from repo_repro.core.urls import repository_url

if TYPE_CHECKING:
    pass


def validate_ref(ref: str) -> str:
    if (
        not re.fullmatch(r"[A-Za-z0-9_./-]+", ref)
        or ref.startswith(("-", "/"))
        or ".." in ref
        or ref.endswith("/")
    ):
        raise InputError("unsupported Git ref; use a branch, tag, or full commit ID")
    return ref


def git_environment() -> dict[str, str]:
    names = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in names}
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_ALLOW_PROTOCOL": "https",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": "core.fsmonitor",
            "GIT_CONFIG_VALUE_1": "false",
        }
    )
    return environment


class GitRepository:
    def __init__(
        self,
        *,
        timeout: float = 120,
        max_files: int = 10_000,
        max_file_bytes: int = 1_048_576,
        max_read_bytes: int = 8_388_608,
    ) -> None:
        self.timeout = timeout
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.max_read_bytes = max_read_bytes

    def clone(self, url: str, destination: Path, *, ref: str | None = None) -> Path:
        normalized = repository_url(url)
        if ref is not None:
            validate_ref(ref)
        destination = destination.absolute()
        if destination.exists() or destination.is_symlink():
            raise RepositoryError("clone destination must not already exist", stage="clone")
        executable = shutil.which("git")
        if executable is None:
            raise RepositoryError("Git is not installed", stage="clone")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="repro-git-") as configuration:
            options = [
                executable,
                "-c",
                f"core.hooksPath={configuration}",
                "-c",
                "credential.helper=",
                "-c",
                "protocol.allow=never",
                "-c",
                "protocol.https.allow=always",
                "-c",
                "http.followRedirects=false",
                "-c",
                "core.fsmonitor=false",
            ]
            self._command(
                [
                    *options,
                    "clone",
                    "--bare",
                    "--depth=1",
                    "--single-branch",
                    "--no-tags",
                    "--quiet",
                    f"--template={configuration}",
                    "--",
                    normalized,
                    str(destination),
                ]
            )
            if ref is not None:
                self._command(
                    [
                        *options,
                        "-C",
                        str(destination),
                        "fetch",
                        "--quiet",
                        "--depth=1",
                        "origin",
                        ref,
                    ]
                )
        return destination

    def _command(self, arguments: list[str]) -> None:
        try:
            result = subprocess.run(
                arguments,
                capture_output=True,
                timeout=self.timeout,
                env=git_environment(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RepositoryError("Git acquisition failed or timed out", stage="clone") from error
        if result.returncode:
            raise RepositoryError(
                "Git could not fetch the requested public repository/ref", stage="clone"
            )

    def open(self, path: Path, *, ref: str = "HEAD") -> "GitTreeView":
        return GitTreeView(path, validate_ref(ref), self)


class GitTreeView:
    def __init__(self, path: Path, ref: str, limits: GitRepository) -> None:
        # Lazy import: package import never starts GitPython's Git executable probe.
        from git import Repo
        from git.exc import BadName, BadObject, GitError
        from git.objects.blob import Blob
        from git.objects.tree import Tree

        self._repo: Repo
        self._blobs: dict[str, Blob] = {}
        self._limits = limits
        self._read_bytes = 0
        self.warnings: list[str] = []
        try:
            self._repo = Repo(path, search_parent_directories=False, expand_vars=False)
            git_directory = Path(self._repo.git_dir)
            if (git_directory / "objects/info/alternates").exists() or any(
                (git_directory / "objects/pack").glob("*.promisor")
            ):
                raise RepositoryError("partial clones and external object stores are unsupported")
            safe = git_environment()
            self._repo.git.update_environment(
                **{key: safe.get(key) for key in set(os.environ) | set(safe)}
            )
            self.commit = self._repo.commit(ref).hexsha
            self.origin_url: str | None = None
            if "origin" in self._repo.remotes:
                try:
                    self.origin_url = repository_url(self._repo.remotes.origin.url)
                except InputError:
                    self.warnings.append("unsafe or unsupported origin URL was ignored")
            self.default_branch = (
                self._repo.head.reference.name
                if self._repo.bare and not self._repo.head.is_detached
                else None
            )
            pending = [self._repo.commit(self.commit).tree]
            count = 0
            while pending:
                for item in pending.pop():
                    count += 1
                    if count > limits.max_files:
                        raise RepositoryError("repository file count exceeds the analysis limit")
                    if isinstance(item, Tree):
                        pending.append(item)
                        continue
                    name = os.fspath(item.path)
                    ArtifactRef.validate_path(name)
                    if not isinstance(item, Blob) or stat.S_ISLNK(item.mode):
                        self.warnings.append(f"unsupported link/submodule: {name}")
                        continue
                    self._blobs[name] = item
        except RepositoryError:
            self.close()
            raise
        except (GitError, BadName, BadObject, OSError, ValueError) as error:
            self.close()
            raise RepositoryError(
                "cannot open repository or resolve a safe Git tree", stage="analyze"
            ) from error

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._blobs))

    def read(self, path: str) -> bytes:
        from git.exc import BadObject, GitError

        ArtifactRef.validate_path(path)
        if path not in self._blobs:
            raise RepositoryError("requested path is absent from the analyzed Git tree")
        blob = self._blobs[path]
        try:
            if blob.size > self._limits.max_file_bytes:
                raise RepositoryError(f"analysis file is too large: {path}")
            self._read_bytes += blob.size
            if self._read_bytes > self._limits.max_read_bytes:
                raise RepositoryError("analysis read budget exceeded")
            data = bytes(blob.data_stream.read(self._limits.max_file_bytes + 1))
            if len(data) != blob.size:
                raise RepositoryError("Git returned an incomplete blob")
            return data
        except (GitError, BadObject, OSError, ValueError) as error:
            raise RepositoryError(
                "could not read the Git object safely", stage="analyze"
            ) from error

    def close(self) -> None:
        if hasattr(self, "_repo"):
            self._repo.close()

    def __enter__(self) -> "GitTreeView":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
