"""Small infrastructure ports, independent of CLI and execution backends."""

from typing import Protocol

from repo_repro.core.models import ArtifactRef


class ArtifactStore(Protocol):
    """Store bytes by content and verify references when reading them."""

    def put_bytes(self, data: bytes, *, media_type: str) -> ArtifactRef:
        """Persist bytes atomically and return their verified content reference."""
        ...

    def read_bytes(self, reference: ArtifactRef) -> bytes:
        """Read referenced bytes after checking the path, size, and digest."""
        ...
