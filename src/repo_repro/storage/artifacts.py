"""Atomic, content-addressed files; containers never receive this directory."""

import hashlib
import os
import tempfile
from pathlib import Path

from repo_repro.core.exceptions import RepoReproError
from repo_repro.core.models import ArtifactRef


class FileArtifactStore:
    def __init__(self, root: Path, *, max_artifact_bytes: int = 67_108_864) -> None:
        if max_artifact_bytes < 1:
            raise ValueError("artifact size limit must be positive")
        self.max_artifact_bytes = max_artifact_bytes
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(self, data: bytes, *, media_type: str) -> ArtifactRef:
        if len(data) > self.max_artifact_bytes:
            raise RepoReproError("artifact exceeds size limit", stage="storage")
        digest = hashlib.sha256(data).hexdigest()
        reference = ArtifactRef(
            path=f"objects/{digest}",
            sha256=digest,
            size_bytes=len(data),
            media_type=media_type,
        )
        target = self._path(reference)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                temporary = stream.name
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            return reference
        except OSError as error:
            raise RepoReproError("cannot persist execution evidence", stage="storage") from error
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def _path(self, reference: ArtifactRef) -> Path:
        target = self.root / reference.path
        if target.is_symlink() or not target.resolve().is_relative_to(self.root):
            raise RepoReproError("artifact path escaped storage", stage="storage")
        return target

    def read_bytes(self, reference: ArtifactRef) -> bytes:
        if reference.size_bytes > self.max_artifact_bytes:
            raise RepoReproError("artifact exceeds size limit", stage="storage")
        try:
            with self._path(reference).open("rb") as stream:
                data = stream.read(reference.size_bytes + 1)
        except OSError as error:
            raise RepoReproError("cannot read execution evidence", stage="storage") from error
        if (
            len(data) != reference.size_bytes
            or hashlib.sha256(data).hexdigest() != reference.sha256
        ):
            raise RepoReproError("artifact integrity check failed", stage="storage")
        return data
