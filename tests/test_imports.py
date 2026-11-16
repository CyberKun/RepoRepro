"""Every distributed module must import without starting application work."""

import importlib
import pkgutil

import pytest

import repo_repro

MODULE_NAMES = tuple(
    module.name for module in pkgutil.walk_packages(repo_repro.__path__, prefix="repo_repro.")
)


@pytest.mark.parametrize("module_name", MODULE_NAMES)
def test_all_modules_import(module_name: str, capsys: pytest.CaptureFixture[str]) -> None:
    module = importlib.import_module(module_name)

    assert module.__name__ == module_name
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
