"""Configuration construction is portable and never creates storage directories."""

import sys
from pathlib import Path

import pytest

from repo_repro import config
from repo_repro.config import AppConfig


def test_explicit_config_does_not_create_storage(tmp_path: Path) -> None:
    data_dir = tmp_path / "not-created"

    settings = AppConfig(data_dir=data_dir)

    assert settings.data_dir == data_dir
    assert settings.sandbox_policy is None
    assert not data_dir.exists()


@pytest.mark.parametrize(
    ("platform", "variable", "suffix"),
    [("win32", "LOCALAPPDATA", "repo-repro"), ("linux", "XDG_DATA_HOME", "repo-repro")],
)
def test_default_directory_respects_platform_data_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
    variable: str,
    suffix: str,
) -> None:
    root = tmp_path / "data"
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv(variable, str(root))

    settings = AppConfig()

    assert settings.data_dir == root / suffix
    assert not root.exists()


def test_macos_uses_application_support(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert config.default_data_dir() == tmp_path / "Library" / "Application Support" / "repo-repro"
