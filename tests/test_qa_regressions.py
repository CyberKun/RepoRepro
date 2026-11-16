"""Independent QA regressions for malformed input and false reproduction evidence."""

import io
import json
import struct
import tarfile
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repo_repro.capsule.archive import extract_capsule, validate_capsule
from repo_repro.capsule.images import scan_image
from repo_repro.capsule.schema import CapsuleError
from repo_repro.capsule.security import SecretRedactor
from repo_repro.cli import app
from repo_repro.core.exceptions import PolicyViolation
from repo_repro.reproduction.safety import validate_proposal
from tests.repro_support import context, proposal
from tests.test_capsule_format import diagnostic_capsule


@pytest.mark.parametrize(
    "body",
    [
        "    parse_value = int\n    assert parse_value('1') == 0, 'returns zero'\n",
        "    assert False and parse_value('') == 0, 'returns zero'\n",
        "    assert True or parse_value('') == 0, 'returns zero'\n",
        "    result = parse_value('')\n    result *= 0\n    assert result == 1, 'returns zero'\n",
        "    result = parse_value('')\n    del result\n    assert result == 0\n",
        "    result = [parse_value('') for _ in []]\n    assert result == [0]\n",
        "    assert (parse_value := int)('1') == 0, 'returns zero'\n",
        "    result = parse_value('')\n    result.clear()\n    assert len(result) == 1\n",
        "    result = parse_value('')\n    result[0] = 99\n    assert result[0] == 0\n",
    ],
)
def test_manufactured_or_short_circuited_failures_are_rejected(body: str) -> None:
    candidate = proposal().model_copy(
        update={"test_code": "from calculator import parse_value\ndef test_repro():\n" + body}
    )
    with pytest.raises(PolicyViolation):
        validate_proposal(candidate, context())


def test_function_annotation_cannot_execute_a_repository_call_at_collection() -> None:
    code = (
        "from calculator import parse_value\n"
        "def test_repro() -> parse_value(''):\n"
        "    assert parse_value('') == 0, 'returns zero'\n"
    )
    with pytest.raises(PolicyViolation):
        validate_proposal(proposal().model_copy(update={"test_code": code}), context())


def compressed_corruption(tmp_path: Path) -> Path:
    original = tmp_path / "original.zip"
    diagnostic_capsule(original)
    packed = tmp_path / "deflated.zip"
    with (
        zipfile.ZipFile(original) as source,
        zipfile.ZipFile(packed, "w", compression=zipfile.ZIP_DEFLATED) as output,
    ):
        for member in source.infolist():
            output.writestr(member.filename, source.read(member))
    with zipfile.ZipFile(packed) as archive:
        offset = archive.getinfo("reproduction/repro.yaml").header_offset
    data = bytearray(packed.read_bytes())
    name_size, extra_size = struct.unpack_from("<HH", data, offset + 26)
    start = offset + 30 + name_size + extra_size
    data[start : start + 2] = b"\xff\xff"
    packed.write_bytes(data)
    return packed


def test_invalid_deflate_is_a_typed_capsule_error(tmp_path: Path) -> None:
    packed = compressed_corruption(tmp_path)
    with pytest.raises(CapsuleError):
        validate_capsule(packed)
    destination = tmp_path / "unpacked"
    with pytest.raises(CapsuleError):
        extract_capsule(packed, destination)
    assert not destination.exists()


def test_invalid_deflate_cli_has_a_useful_error(tmp_path: Path) -> None:
    packed = compressed_corruption(tmp_path)
    result = CliRunner().invoke(app, ["capsule", "validate", str(packed)])
    assert result.exit_code == 3
    assert "invalid ZIP capsule" in result.output


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("manifest.json", [None]),
        ("manifest.json", [123]),
        ("index.json", {"manifests": [None]}),
        ("index.json", {"manifests": "x"}),
        ("index.json", {"manifests": None}),
    ],
)
def test_malformed_image_metadata_is_rejected(tmp_path: Path, name: str, payload: object) -> None:
    path = tmp_path / "image.tar"
    data = json.dumps(payload).encode()
    with tarfile.open(path, "w") as output:
        member = tarfile.TarInfo(name)
        member.size = len(data)
        output.addfile(member, io.BytesIO(data))
    with pytest.raises(CapsuleError):
        scan_image(path, SecretRedactor(include_environment=False))


@pytest.mark.parametrize("name", ["config.py", "data.bin", "credentials.pem"])
def test_image_private_keys_are_rejected_in_all_file_types(tmp_path: Path, name: str) -> None:
    secret = b"-----BEGIN PRIVATE KEY-----\nself-owned-fixture\n-----END PRIVATE KEY-----"
    layer_buffer = io.BytesIO()
    with tarfile.open(fileobj=layer_buffer, mode="w") as layer:
        member = tarfile.TarInfo(name)
        member.size = len(secret)
        layer.addfile(member, io.BytesIO(secret))
    path = tmp_path / "image.tar"
    with tarfile.open(path, "w") as image:
        content = layer_buffer.getvalue()
        member = tarfile.TarInfo("fixture/layer.tar")
        member.size = len(content)
        image.addfile(member, io.BytesIO(content))
    with pytest.raises(CapsuleError):
        scan_image(path, SecretRedactor(include_environment=False))


def test_binary_pem_marker_constants_are_not_private_key_payloads() -> None:
    redactor = SecretRedactor(include_environment=False)
    assert not redactor.binary_sensitive(b"\0-----BEGIN PRIVATE KEY-----\0")
    assert not redactor.binary_sensitive(b"\0-----BEGIN PRIVATE KEY-----\n\0")
    assert redactor.binary_sensitive(b"\0-----BEGIN PRIVATE KEY-----\nfixture\n")


@pytest.mark.parametrize("scheme", ["postgresql", "mongodb+srv", "redis"])
def test_database_urls_are_redacted_without_a_known_secret(scheme: str) -> None:
    redactor = SecretRedactor(include_environment=False)
    raw = f"connection={scheme}://username:opaque-database-qa@example.invalid/database"
    assert "opaque-database-qa" not in redactor.text(raw)
    with pytest.raises(CapsuleError):
        redactor.critical(raw.encode())


def test_encrypted_private_key_payload_is_redacted() -> None:
    raw = (
        "-----BEGIN ENCRYPTED PRIVATE KEY-----\nopaque-encrypted-qa\n"
        "-----END ENCRYPTED PRIVATE KEY-----"
    )
    assert "opaque-encrypted-qa" not in SecretRedactor(include_environment=False).text(raw)


def test_diagnostic_environment_metadata_is_redacted(tmp_path: Path) -> None:
    from repo_repro.capsule.exporter import CapsuleExporter
    from repo_repro.storage.artifacts import FileArtifactStore
    from tests.test_capsule_io import captured

    store = FileArtifactStore(tmp_path / "private")
    result = captured(store)
    path = tmp_path / "diagnostic.zip"
    CapsuleExporter(
        store, redactor=SecretRedactor((result.environment.image,), include_environment=False)
    ).export(result, path, diagnostic_only=True)
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            assert result.environment.image.encode() not in archive.read(member)


@pytest.mark.parametrize("command", ["issue", "analyze"])
def test_invalid_github_token_is_a_controlled_cli_error(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "private\ninvalid-header")
    result = CliRunner().invoke(app, [command, "https://github.com/o/r/issues/1"])
    assert result.exit_code == 2
    assert "invalid GitHub token" in result.output
    assert "private" not in result.output
