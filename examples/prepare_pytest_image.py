"""Prepare Python 3.11 test images from a reviewed base and pure wheels, without running them."""

import argparse
import hashlib
import io
import json
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from uuid import uuid4

from repo_repro.core.models import ArtifactRef
from repo_repro.sandbox.docker_runner import DockerCLI

PACKAGING = {"pip", "setuptools"}
FRAMEWORK = {"pytest", "pluggy", "pygments", "iniconfig", "packaging", "colorama"}
SITE = "usr/local/lib/python3.11/site-packages/"


def wheel_files(
    wheelhouse: Path,
) -> tuple[dict[str, bytes], dict[str, bytes], list[dict[str, str]]]:
    packaging: dict[str, bytes] = {}
    framework: dict[str, bytes] = {}
    records: list[dict[str, str]] = []
    packages: set[str] = set()
    total = 0
    for wheel in sorted(wheelhouse.glob("*.whl")):
        package = wheel.name.split("-", 1)[0].lower()
        if package not in PACKAGING | FRAMEWORK or not wheel.name.endswith("-none-any.whl"):
            raise ValueError("supply only the named pure-Python packaging and pytest wheels")
        if package in packages:
            raise ValueError("supply exactly one version of each package")
        packages.add(package)
        records.append(
            {"wheel": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
        )
        files = packaging if package in PACKAGING else framework
        with zipfile.ZipFile(wheel) as archive:
            for member in archive.infolist():
                name = member.filename.removesuffix("/")
                ArtifactRef.validate_path(name)
                if stat.S_ISLNK(member.external_attr >> 16) or ".data" in name.split("/")[0]:
                    raise ValueError("wheel links and relocated .data files are unsupported")
                if member.is_dir():
                    continue
                total += member.file_size
                if total > 67_108_864 or member.file_size > 8_388_608 or len(files) >= 10_000:
                    raise ValueError("fixture wheel size budget exceeded")
                if name in files:
                    raise ValueError("wheel files collide")
                files[name] = archive.read(member)
    if not PACKAGING | {"pytest"} <= packages:
        raise ValueError("supply pip, setuptools, pytest and its dependencies")
    return packaging, framework, records


def omit(path: str) -> bool:
    if PurePosixPath(path).name.startswith("libgnutls.so."):
        # GnuTLS embeds public self-test private keys; keep the Capsule scanner strict.
        return True
    if path.startswith(SITE):
        top = path.removeprefix(SITE).split("/", 1)[0]
        return top in {"pip", "setuptools", "pkg_resources", "_distutils_hack"} or top.startswith(
            ("pip-", "setuptools-", "distutils-precedence.pth")
        )
    return False


def overlay_rootfs(source: Path, destination: Path, files: dict[str, bytes]) -> list[str]:
    omitted: list[str] = []
    total = 0
    has_python = False
    with tarfile.open(source, "r:") as archive, tarfile.open(destination, "w") as output:
        for member in archive:
            name = member.name.removeprefix("./")
            if PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
                raise ValueError("unsafe base filesystem path")
            has_python |= name.startswith("usr/local/lib/python3.11/")
            total += member.size
            if total > 2_147_483_648:
                raise ValueError("base filesystem exceeds the fixture size budget")
            if omit(name):
                omitted.append(name)
                continue
            stream = archive.extractfile(member) if member.isfile() else None
            try:
                output.addfile(member, stream)
            finally:
                if stream is not None:
                    stream.close()
        if not has_python:
            raise ValueError("the fixture builder requires /usr/local/lib/python3.11")
        for name, content in sorted(files.items()):
            ArtifactRef.validate_path(name)
            member = tarfile.TarInfo(SITE + name)
            member.size, member.mode, member.mtime = len(content), 0o644, 0
            output.addfile(member, io.BytesIO(content))
    return omitted


def prepare_images(base_image: str, wheelhouse: Path, workspace: Path) -> dict[str, str]:
    cli = DockerCLI()

    def run(arguments: list[str], timeout: float = 120) -> bytes:
        result = cli.run(arguments, timeout=timeout)
        if result.returncode or result.timed_out or result.output_limited:
            raise RuntimeError(
                f"Docker {arguments[0]} failed: " + result.stderr.decode(errors="replace")
            )
        return result.stdout

    inspected = json.loads(run(["image", "inspect", base_image]))
    if (
        len(inspected) != 1
        or inspected[0].get("Os") != "linux"
        or inspected[0].get("Architecture") != "amd64"
    ):
        raise ValueError("use a locally cached Linux amd64 Python 3.11 base image")
    identity = str(inspected[0]["Id"])
    packaging, framework, records = wheel_files(wheelhouse)
    directory = workspace.resolve() / uuid4().hex
    directory.mkdir(parents=True)
    name = "repo-repro-image-prep-" + directory.name
    run(["create", "--name", name, "--network=none", "--entrypoint=/bin/true", identity])
    try:
        run(["export", "--output", str(directory / "rootfs.tar"), name])
    finally:
        run(["rm", name])
    omitted = overlay_rootfs(directory / "rootfs.tar", directory / "python.tar", packaging)
    overlay_rootfs(directory / "rootfs.tar", directory / "pytest.tar", packaging | framework)
    images = {
        key: run(["import", "--platform=linux/amd64", str(directory / filename)]).decode().strip()
        for key, filename in (("base_image", "python.tar"), ("pytest_image", "pytest.tar"))
    }
    (directory / "build-record.json").write_text(
        json.dumps(
            {"base_image_id": identity, "images": images, "wheels": records, "omitted": omitted},
            indent=2,
        ),
        encoding="utf-8",
    )
    return images


def prepare(base_image: str, wheelhouse: Path, workspace: Path) -> str:
    return prepare_images(base_image, wheelhouse, workspace)["pytest_image"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--wheelhouse", required=True, type=Path)
    parser.add_argument("--workspace", type=Path, default=Path(".tools/pytest-image-build"))
    parser.add_argument(
        "--json", action="store_true", help="Print both immutable image IDs as JSON"
    )
    args = parser.parse_args()
    images = prepare_images(args.base_image, args.wheelhouse, args.workspace)
    print(json.dumps(images) if args.json else images["pytest_image"])


if __name__ == "__main__":
    main()
