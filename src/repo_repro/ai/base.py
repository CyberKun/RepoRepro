"""Vendor-neutral, generation-only model port."""

from dataclasses import dataclass
from typing import Protocol

from repo_repro.core.exceptions import RepoReproError
from repo_repro.core.models import ArtifactRef


@dataclass(frozen=True)
class ModelReply:
    content: str
    audit: ArtifactRef


class AIProvider(Protocol):
    def complete(self, system: str, user: str) -> ModelReply: ...
    def redact(self, text: str) -> str: ...


class ProviderError(RepoReproError):
    code = "MODEL_REQUEST_FAILED"

    def __init__(self, message: str, audit: ArtifactRef) -> None:
        super().__init__(message, stage="generation")
        self.audit = audit
