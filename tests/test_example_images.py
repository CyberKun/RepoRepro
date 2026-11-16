"""The documented image helper must preserve ordinary runtime data and reject unsafe wheels."""

import io
import tarfile
import zipfile
from pathlib import Path

import pytest
from examples.prepare_pytest_image import SITE, omit, overlay_rootfs, wheel_files


def wheel(path: Path, member: str, content: bytes = b"fixture") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member, content)


def test_wheels_are_separated_into_packaging_and_framework(tmp_path: Path) -> None:
    for package in ("pip", "setuptools", "pytest"):
        wheel(tmp_path / f"{package}-1.0-py3-none-any.whl", f"{package}/__init__.py")
    packaging, framework, records = wheel_files(tmp_path)
    assert set(packaging) == {"pip/__init__.py", "setuptools/__init__.py"}
    assert set(framework) == {"pytest/__init__.py"}
    assert len(records) == 3 and all(len(record["sha256"]) == 64 for record in records)


@pytest.mark.parametrize("member", ["../outside", "/absolute", "pkg.data/scripts/command"])
def test_unsafe_wheel_members_are_rejected(tmp_path: Path, member: str) -> None:
    wheel(tmp_path / "pip-1.0-py3-none-any.whl", member)
    with pytest.raises(ValueError):
        wheel_files(tmp_path)


def test_multiple_versions_of_a_wheel_are_rejected(tmp_path: Path) -> None:
    for version in ("1.0", "2.0"):
        wheel(tmp_path / f"pip-{version}-py3-none-any.whl", "pip/__init__.py")
    with pytest.raises(ValueError, match="one version"):
        wheel_files(tmp_path)


def test_missing_packaging_wheels_are_rejected(tmp_path: Path) -> None:
    wheel(tmp_path / "pytest-1.0-py3-none-any.whl", "pytest/__init__.py")
    with pytest.raises(ValueError, match="supply pip"):
        wheel_files(tmp_path)


@pytest.mark.parametrize("package", ["unknown-1.0-py3-none-any.whl", "pip-1.0-cp311-linux.whl"])
def test_unapproved_wheel_types_are_rejected(tmp_path: Path, package: str) -> None:
    wheel(tmp_path / package, "package.py")
    with pytest.raises(ValueError, match="named pure-Python"):
        wheel_files(tmp_path)


def test_rootfs_overlay_replaces_old_packages_and_omits_embedded_keys(tmp_path: Path) -> None:
    source, target = tmp_path / "source.tar", tmp_path / "target.tar"
    files = {
        "usr/local/lib/python3.11/os.py": b"stdlib",
        SITE + "pip/__init__.py": b"old packaging",
        "usr/lib/libgnutls.so.30": b"embedded public test keys",
        "usr/lib/libssl.so.3": b"required library",
    }
    with tarfile.open(source, "w") as archive:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    omitted = overlay_rootfs(source, target, {"pip/__init__.py": b"updated packaging"})
    with tarfile.open(target) as archive:
        assert "usr/lib/libgnutls.so.30" not in archive.getnames()
        assert archive.getnames().count(SITE + "pip/__init__.py") == 1
        for name, expected in (
            (SITE + "pip/__init__.py", b"updated packaging"),
            ("usr/lib/libssl.so.3", b"required library"),
        ):
            stream = archive.extractfile(name)
            assert stream is not None and stream.read() == expected
    assert "usr/lib/libgnutls.so.30" in omitted
    assert not omit("usr/local/bin/python3")


def test_wrong_python_rootfs_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "empty.tar"
    with tarfile.open(source, "w"):
        pass
    with pytest.raises(ValueError, match=r"python3\.11"):
        overlay_rootfs(source, tmp_path / "target.tar", {})
