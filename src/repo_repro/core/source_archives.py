"""Bounded plain-file source snapshots shared by sandbox and capsule transports."""

import io
import tarfile
import unicodedata

from repo_repro.core.exceptions import PolicyViolation
from repo_repro.core.models import ArtifactRef


def safe_path(value: str) -> str:
    ArtifactRef.validate_path(value)
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("source paths must use NFC normalization")
    return value


def source_files(content: bytes) -> dict[str, bytes]:
    """Read regular USTAR/PAX members without extracting or following links."""
    if len(content) > 33_554_432:
        raise PolicyViolation("source snapshot exceeds 32 MiB")
    result: dict[str, bytes] = {}
    aliases: set[str] = set()
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
            for member in archive:
                safe_path(member.name)
                if (
                    not member.isfile()
                    or member.name.casefold() in aliases
                    or member.size > 8_388_608
                ):
                    raise PolicyViolation(
                        "source snapshot has links, duplicate or oversized members"
                    )
                aliases.add(member.name.casefold())
                if len(aliases) > 5000:
                    raise PolicyViolation("source snapshot has too many files")
                total += member.size
                if total > 33_554_432:
                    raise PolicyViolation("source snapshot expands beyond budget")
                stream = archive.extractfile(member)
                if stream is None:
                    raise PolicyViolation("source file has no data")
                with stream:
                    data = stream.read(member.size + 1)
                if len(data) != member.size:
                    raise PolicyViolation("incomplete source file")
                result[member.name] = data
    except (tarfile.TarError, OSError, ValueError) as error:
        raise PolicyViolation("invalid or unsafe source snapshot") from error
    if not result:
        raise PolicyViolation("empty source snapshot")
    for path in result:
        if any(
            ("/".join(path.split("/")[:i])).casefold() in aliases
            for i in range(1, len(path.split("/")))
        ):
            raise PolicyViolation("source file/directory conflict")
    return result


def pack_source(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, content in sorted(files.items()):
            safe_path(name)
            header = tarfile.TarInfo(name)
            header.size, header.mode, header.mtime = len(content), 0o644, 0
            archive.addfile(header, io.BytesIO(content))
    if output.tell() > 33_554_432:
        raise PolicyViolation("source snapshot exceeds 32 MiB")
    return output.getvalue()
