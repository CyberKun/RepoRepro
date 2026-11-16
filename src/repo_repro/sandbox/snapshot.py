"""Copy committed regular Git blobs into a bounded archive, never host mounts."""

import hashlib
import io
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from repo_repro.analyzer.repository import GitRepository
from repo_repro.core.exceptions import PolicyViolation
from repo_repro.core.models import ArtifactRef

_EXCLUDED = frozenset(
    {
        ".git",
        ".ssh",
        ".aws",
        ".azure",
        ".docker",
        ".config",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)


@dataclass(frozen=True)
class SourceSnapshot:
    commit: str
    archive: bytes
    excluded: tuple[str, ...]
    injected: tuple[tuple[str, str], ...] = ()


def snapshot_repository(
    path: Path,
    *,
    max_bytes: int = 33_554_432,
    ref: str = "HEAD",
    injected: Mapping[str, bytes] | None = None,
) -> SourceSnapshot:
    root = path.resolve(strict=True)
    home = Path.home().resolve()
    if (
        root == home
        or root in home.parents
        or any(root.is_relative_to(home / name) for name in (".ssh", ".aws", ".azure", ".docker"))
    ):
        raise PolicyViolation(
            "sensitive host directories cannot be repository inputs", stage="sandbox"
        )
    source = GitRepository(max_files=5000, max_file_bytes=8_388_608, max_read_bytes=max_bytes)
    buffer = io.BytesIO()
    excluded: list[str] = []
    with source.open(root, ref=ref) as view:
        if view.warnings:
            raise PolicyViolation(
                "sandbox snapshots do not support links or submodules", stage="sandbox"
            )
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            if injected and any(
                name == "repo_repro_generated" or name.startswith("repo_repro_generated/")
                for name in view.paths
            ):
                raise PolicyViolation("repository collides with reserved test support directory")
            if len(view.paths) + len(injected or {}) > 5000:
                raise PolicyViolation("snapshot file count exceeds limit")
            for name in view.paths:
                parts = PurePosixPath(name).parts
                if any(part in _EXCLUDED or part.startswith(".env") for part in parts) or (
                    PurePosixPath(name).name
                    in {"id_rsa", "id_ed25519", "credentials", "config.json"}
                ):
                    excluded.append(name)
                    continue
                data = view.read(name)
                header = tarfile.TarInfo(name)
                header.size = len(data)
                header.mode = 0o644
                header.mtime = 0
                archive.addfile(header, io.BytesIO(data))
                if buffer.tell() > max_bytes:
                    raise PolicyViolation("repository snapshot exceeds size limit", stage="sandbox")
            for name, data in sorted((injected or {}).items()):
                ArtifactRef.validate_path(name)
                if not name.startswith("repo_repro_generated/"):
                    raise PolicyViolation("test injection is restricted to the reserved directory")
                header = tarfile.TarInfo(name)
                header.size = len(data)
                header.mode = 0o644
                archive.addfile(header, io.BytesIO(data))
        if buffer.tell() > max_bytes:
            raise PolicyViolation("repository snapshot exceeds size limit", stage="sandbox")
        return SourceSnapshot(
            view.commit,
            buffer.getvalue(),
            tuple(excluded),
            tuple(
                (name, hashlib.sha256(data).hexdigest())
                for name, data in sorted((injected or {}).items())
            ),
        )
