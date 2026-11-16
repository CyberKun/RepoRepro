"""Stable Capsule 1.0.x contracts. No parsing or importing operation executes code."""

import json
import unicodedata
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from repo_repro import __version__
from repo_repro.core.exceptions import RepoReproError
from repo_repro.core.models import ArtifactRef, Record
from repo_repro.core.urls import repository_url
from repo_repro.reproduction.models import FailureOracle

SCHEMA_VERSION = "1.0.0"
COMMAND = ("python", "-I", "/workspace/repo_repro_generated/launch.py")
REQUIRED = {
    "README.md",
    "Dockerfile",
    "reproduce.sh",
    "environment.json",
    "report.md",
    "tests/test_reproduction.py",
    "evidence/stdout.log",
    "evidence/stderr.log",
    "evidence/trace.json",
    "reference/result.json",
}


class CapsuleError(RepoReproError):
    code = "CAPSULE_INVALID"

    def __init__(self, message: str) -> None:
        super().__init__(message, stage="capsule")


def safe_path(value: str) -> str:
    ArtifactRef.validate_path(value)
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("capsule paths must be NFC normalized")
    return value


def json_object(content: bytes) -> object:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise CapsuleError("duplicate metadata key")
            result[key] = value
        return result

    def invalid(value: str) -> object:
        raise CapsuleError("non-finite JSON values are forbidden")

    try:
        return json.loads(content.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise CapsuleError("metadata must use the Capsule JSON profile of YAML 1.2") from error


class FileRole(StrEnum):
    REPLAY = "replay_data"
    DIAGNOSTIC = "diagnostic"


class CapsuleFile(ArtifactRef):
    role: FileRole

    @field_validator("path")
    @classmethod
    def portable_path(cls, value: str) -> str:
        return safe_path(value)


class CapsuleSource(Record):
    side: Literal["original", "fixed"]
    commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    path: str

    @field_validator("path")
    @classmethod
    def source_path(cls, value: str) -> str:
        safe_path(value)
        if value not in {"sources/original.tar", "sources/fixed.tar"}:
            raise ValueError("unexpected source snapshot path")
        return value


class CapsuleImage(Record):
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    platform: Literal["linux/amd64"] = "linux/amd64"
    path: Literal["images/runtime.tar"] = "images/runtime.tar"


class ReplayRecipe(Record):
    test_id: UUID
    test_path: Literal["tests/test_reproduction.py"] = "tests/test_reproduction.py"
    test_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    oracle: FailureOracle
    helper_hashes: tuple[tuple[str, str], ...]


class CapsuleManifest(Record):
    schema_version: str = Field(pattern=r"^1\.0\.(?:0|[1-9][0-9]*)$")
    format: Literal["repo-repro-capsule"] = "repo-repro-capsule"
    capsule_id: UUID
    producer_version: str = __version__
    mode: Literal["offline", "diagnostic"]
    repository_url: str
    original_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    fixed_commit: str | None
    programming_language: Literal["python"]
    runtime_version: str
    test_framework: Literal["pytest"]
    reproduction_command: tuple[str, ...]
    timeout: int = Field(ge=1, le=3600)
    expected_behavior: str
    observed_behavior: str
    execution_status: str
    generated_at: AwareDatetime
    files: tuple[CapsuleFile, ...]
    sources: tuple[CapsuleSource, ...] = ()
    image: CapsuleImage | None = None
    replay: ReplayRecipe | None = None

    @field_validator("repository_url")
    @classmethod
    def safe_repository(cls, value: str) -> str:
        return value if value == "local:git-snapshot" else repository_url(value)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        paths = [item.path for item in self.files]
        if len({path.casefold() for path in paths}) != len(paths) or not REQUIRED.issubset(paths):
            raise ValueError("duplicate paths or missing standard files")
        if {"repro.yaml", "checksums.sha256"}.intersection(paths):
            raise ValueError("self-referential catalog")
        if self.fixed_commit is not None:
            if len(self.fixed_commit) not in {40, 64} or any(
                char not in "0123456789abcdef" for char in self.fixed_commit
            ):
                raise ValueError("invalid fixed commit")
            if self.fixed_commit == self.original_commit:
                raise ValueError("original and fixed commits must differ")
        if self.reproduction_command != COMMAND:
            raise ValueError("v1 only supports the local trusted pytest launcher")
        catalog = {item.path: item for item in self.files}
        if self.mode == "diagnostic":
            if (
                self.image
                or self.sources
                or self.replay
                or any(item.role is FileRole.REPLAY for item in self.files)
            ):
                raise ValueError("diagnostic capsules cannot carry executable recipes")
        else:
            if self.image is None or self.replay is None:
                raise ValueError("offline capsule requires an image and a sealed test")
            expected = {"original": self.original_commit}
            if self.fixed_commit:
                expected["fixed"] = self.fixed_commit
            if tuple(source.side for source in self.sources) != tuple(expected):
                raise ValueError("source versions must be ordered original then fixed")
            if any(source.path != f"sources/{source.side}.tar" for source in self.sources):
                raise ValueError("source path/side mismatch")
            if {item.side: item.commit for item in self.sources} != expected or len(
                self.sources
            ) != len(expected):
                raise ValueError("source versions do not match commits")
            critical = {
                "environment.json",
                self.replay.test_path,
                self.image.path,
                *(item.path for item in self.sources),
            }
            if not critical.issubset(catalog) or any(
                catalog[path].role is not FileRole.REPLAY for path in critical
            ):
                raise ValueError("missing or diagnostic replay data")
        return self
