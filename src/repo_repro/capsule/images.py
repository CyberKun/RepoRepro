"""Frozen Docker image archives: bounded scanning, no tag restoration, no Dockerfile builds."""

import copy
import gzip
import io
import json
import tarfile
from pathlib import Path, PurePosixPath
from typing import IO, cast

from repo_repro.capsule.schema import CapsuleError, json_object
from repo_repro.capsule.security import KEY, SecretRedactor
from repo_repro.sandbox.docker_runner import DockerCLI

MAX_IMAGE = 2_147_483_648
MAX_EXPANDED = 4_294_967_296


def _read_scan(stream: IO[bytes], redactor: SecretRedactor, budget: list[int]) -> None:
    tail = b""
    overlap = max([4096, *(len(item.encode()) for item in redactor.secrets)])
    while chunk := stream.read(131_072):
        budget[0] += len(chunk)
        if budget[0] > MAX_EXPANDED:
            raise CapsuleError("image expanded scan exceeds budget")
        if redactor.binary_sensitive(tail + chunk):
            raise CapsuleError("image contains a credential pattern or configured secret")
        tail = (tail + chunk)[-overlap:]


def _config_secrets(value: object, redactor: SecretRedactor) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if (
                key == "mediaType"
                and isinstance(item, str)
                and "layer" in item
                and item
                not in {
                    "application/vnd.oci.image.layer.v1.tar",
                    "application/vnd.oci.image.layer.v1.tar+gzip",
                    "application/vnd.docker.image.rootfs.diff.tar",
                    "application/vnd.docker.image.rootfs.diff.tar.gzip",
                }
            ):
                raise CapsuleError("unsupported image layer encoding cannot be scanned")
            if KEY.search(str(key)) and item not in (None, "", [], {}):
                raise CapsuleError("image metadata contains a sensitive field")
            _config_secrets(item, redactor)
    elif isinstance(value, list):
        for item in value:
            _config_secrets(item, redactor)
    elif isinstance(value, str):
        redactor.critical(value.encode())


def scan_image(path: Path, redactor: SecretRedactor) -> None:
    if path.stat().st_size > MAX_IMAGE:
        raise CapsuleError("image archive exceeds 2 GiB")
    budget = [0]
    count = 0
    try:
        with tarfile.open(path, "r:") as archive:
            for member in archive:
                count += 1
                if count > 100_000 or member.size > MAX_IMAGE:
                    raise CapsuleError("image member budget exceeded")
                name = member.name.removeprefix("./")
                if name == "repositories":
                    raise CapsuleError("image tag repositories are forbidden")
                if PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
                    raise CapsuleError("unsafe image archive path")
                if member.isdir():
                    continue
                if not member.isfile():
                    raise CapsuleError("image transport archive contains links or special files")
                stream = archive.extractfile(member)
                if stream is None:
                    raise CapsuleError("missing image member")
                with stream:
                    prefix = stream.read(512)
                    stream.seek(0)
                    if prefix.lstrip().startswith((b"{", b"[")) and member.size <= 8_388_608:
                        data = stream.read()
                        parsed = json_object(data)
                        _config_secrets(parsed, redactor)
                        if name == "manifest.json" and (
                            not isinstance(parsed, list)
                            or len(parsed) != 1
                            or any(
                                not isinstance(item, dict) or item.get("RepoTags")
                                for item in parsed
                            )
                        ):
                            raise CapsuleError("image archive must describe one untagged image")
                        if name == "index.json":
                            descriptors = (
                                parsed.get("manifests") if isinstance(parsed, dict) else None
                            )
                            if (
                                not isinstance(descriptors, list)
                                or len(descriptors) != 1
                                or not isinstance(descriptors[0], dict)
                            ):
                                raise CapsuleError("image index must contain exactly one image")
                            for descriptor in descriptors:
                                if descriptor.get("annotations"):
                                    raise CapsuleError(
                                        "image archive must not restore name annotations"
                                    )
                    elif (
                        prefix.startswith(b"\x1f\x8b")
                        or prefix[257:262] == b"ustar"
                        or name.endswith("/layer.tar")
                    ):
                        decompressed: IO[bytes] = (
                            cast(IO[bytes], gzip.GzipFile(fileobj=stream))
                            if prefix.startswith(b"\x1f\x8b")
                            else stream
                        )
                        with tarfile.open(fileobj=decompressed, mode="r|") as layer:
                            for item in layer:
                                count += 1
                                if count > 100_000 or item.size > MAX_IMAGE:
                                    raise CapsuleError("image layer member budget exceeded")
                                if item.isfile():
                                    data_stream = layer.extractfile(item)
                                    if data_stream is not None:
                                        with data_stream:
                                            if PurePosixPath(item.name).suffix in {
                                                ".pem",
                                                ".key",
                                            } or PurePosixPath(item.name).name.startswith(
                                                (".env", "id_rsa", "id_ed25519")
                                            ):
                                                if item.size > 8_388_608:
                                                    raise CapsuleError(
                                                        "oversized credential-like image file"
                                                    )
                                                budget[0] += item.size
                                                if budget[0] > MAX_EXPANDED:
                                                    raise CapsuleError(
                                                        "image expanded budget exceeded"
                                                    )
                                                redactor.critical(data_stream.read())
                                                continue
                                            _read_scan(data_stream, redactor, budget)
                    else:
                        _read_scan(stream, redactor, budget)
    except (tarfile.TarError, OSError, ValueError, EOFError) as error:
        raise CapsuleError("invalid or unsafe Docker image archive") from error


def remove_image_tags(source: Path, destination: Path) -> None:
    """Keep config/layer blobs unchanged; remove all loader-level mutable tag bindings."""
    with tarfile.open(source, "r:") as archive, tarfile.open(destination, "w") as output:
        for entry in archive:
            if not entry.isfile():
                if entry.isdir():
                    output.addfile(entry)
                    continue
                raise CapsuleError("unsupported Docker archive entry")
            name = entry.name.removeprefix("./")
            if name == "repositories":
                continue
            stream = archive.extractfile(entry)
            if stream is None:
                raise CapsuleError("incomplete Docker archive")
            with stream:
                if name in {"manifest.json", "index.json"}:
                    if entry.size > 8_388_608:
                        raise CapsuleError("image index exceeds budget")
                    metadata = json_object(stream.read())
                    if name == "manifest.json":
                        if not isinstance(metadata, list):
                            raise CapsuleError("invalid image manifest")
                        for item in metadata:
                            item["RepoTags"] = []
                    elif isinstance(metadata, dict):
                        for item in metadata.get("manifests", []):
                            item.pop("annotations", None)
                    data = json.dumps(metadata).encode()
                    header = copy.copy(entry)
                    header.size = len(data)
                    output.addfile(header, io.BytesIO(data))
                else:
                    output.addfile(entry, stream)


class DockerImageArchive:
    def __init__(self, cli: DockerCLI | None = None) -> None:
        self.cli = cli or DockerCLI()

    def capture(self, image_id: str, destination: Path, redactor: SecretRedactor) -> None:
        inspected = self.cli.run(["image", "inspect", image_id])
        if inspected.returncode or inspected.timed_out:
            raise CapsuleError("cannot inspect captured runtime image")
        info = json.loads(inspected.stdout)
        if len(info) != 1 or int(info[0].get("Size", MAX_IMAGE + 1)) > MAX_IMAGE:
            raise CapsuleError("runtime image exceeds export size budget")
        raw = destination.with_suffix(".unfiltered.tar")
        result = self.cli.run(
            ["image", "save", "--platform=linux/amd64", "--output", str(raw), image_id], timeout=120
        )
        if result.returncode or result.timed_out:
            raise CapsuleError("cannot export frozen image")
        try:
            remove_image_tags(raw, destination)
            scan_image(destination, redactor)
        finally:
            raw.unlink(missing_ok=True)

    def ensure(self, image_id: str, archive: Path, *, force_load: bool = False) -> None:
        current = self.cli.run(["image", "inspect", image_id])
        if current.returncode and b"no such" not in current.stderr.lower():
            raise CapsuleError("Docker image lookup failed")
        if force_load or current.returncode:
            loaded = self.cli.run(["image", "load", "--input", str(archive)], timeout=120)
            if loaded.returncode or loaded.timed_out:
                raise CapsuleError("cannot restore frozen runtime image")
        checked = self.cli.run(["image", "inspect", image_id])
        if checked.returncode:
            raise CapsuleError("restored image ID differs from capsule")
        data = json.loads(checked.stdout)
        if len(data) != 1 or data[0].get("Id") != image_id:
            raise CapsuleError("restored image identity mismatch")
