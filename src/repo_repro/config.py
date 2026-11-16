"""Explicit configuration values without filesystem or environment side effects."""

import os
import sys
from pathlib import Path
from typing import Literal

from pydantic import Field

from repo_repro.core.models import Record, SandboxPolicy


def default_data_dir() -> Path:
    """Return the platform's data directory without creating it."""
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return root / "repo-repro"


class AppConfig(Record):
    """Local configuration; execution requires an explicitly chosen sandbox policy."""

    app_name: Literal["Repo Repro"] = "Repo Repro"
    data_dir: Path = Field(default_factory=default_data_dir)
    sandbox_policy: SandboxPolicy | None = None
