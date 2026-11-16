"""Structured CLI results must survive consoles that cannot encode the original Unicode text."""

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tests.test_capsule_format import diagnostic_capsule

TEXT = "Straße 😀"


def run_legacy_console(arguments: list[str]) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-m", "repo_repro", *arguments],
        capture_output=True,
        check=False,
        env=os.environ | {"PYTHONUTF8": "0", "PYTHONIOENCODING": "gbk"},
        timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode("ascii", errors="replace")
    data: dict[str, object] = json.loads(result.stdout.decode("ascii"))
    return data


def test_issue_json_is_lossless_on_a_legacy_console(tmp_path: Path) -> None:
    issue = tmp_path / "issue.json"
    issue.write_text(
        json.dumps(
            {
                "number": 1,
                "html_url": "https://github.com/o/r/issues/1",
                "title": TEXT,
                "body": TEXT,
            }
        ),
        encoding="utf-8",
    )
    data = run_legacy_console(["issue", "--issue-json", str(issue)])
    assert data["title"] == data["body"] == TEXT


@pytest.mark.parametrize("command", ["inspect", "validate"])
def test_capsule_json_is_lossless_on_a_legacy_console(tmp_path: Path, command: str) -> None:
    original = tmp_path / "original.zip"
    diagnostic_capsule(original)
    with zipfile.ZipFile(original) as archive:
        files = {entry.filename: archive.read(entry) for entry in archive.infolist()}
    manifest = json.loads(files["reproduction/repro.yaml"])
    manifest["expected_behavior"] = TEXT
    files["reproduction/repro.yaml"] = json.dumps(manifest, ensure_ascii=False).encode()
    files["reproduction/checksums.sha256"] = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name.removeprefix('reproduction/')}\n"
        for name, data in sorted(files.items())
        if name != "reproduction/checksums.sha256"
    ).encode()
    capsule = tmp_path / "unicode.zip"
    with zipfile.ZipFile(capsule, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    assert run_legacy_console(["capsule", command, str(capsule)])["expected_behavior"] == TEXT
