"""Static Python/pytest detection using Git objects, TOML, INI and syntax trees."""

import ast
import configparser
import re
import tomllib
from pathlib import Path, PurePosixPath

from repo_repro.analyzer.base import RepositoryView
from repo_repro.analyzer.repository import GitRepository
from repo_repro.core.context import EnvironmentInfo, EvidenceItem, EvidenceOrigin, RepositoryInfo
from repo_repro.core.exceptions import RepositoryError
from repo_repro.core.urls import repository_url


def _table(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


class PythonAnalyzer:
    def __init__(self, source: GitRepository | None = None) -> None:
        self.source = source or GitRepository()

    def clone_and_analyze(
        self, url: str, destination: Path, *, ref: str | None = None
    ) -> RepositoryInfo:
        self.source.clone(url, destination, ref=ref)
        result = self.analyze(destination, url=url, ref="FETCH_HEAD" if ref else "HEAD")
        return result.model_copy(
            update={
                "repository_identity_verified": result.origin_url == repository_url(url),
            }
        )

    def analyze(self, path: Path, *, url: str, ref: str = "HEAD") -> RepositoryInfo:
        with self.source.open(path, ref=ref) as view:
            info = self.inspect(
                view,
                url=repository_url(url),
                commit=view.commit,
                default_branch=view.default_branch,
                ref=ref,
            )
            return info.model_copy(
                update={
                    "warnings": info.warnings + tuple(view.warnings),
                    "origin_url": view.origin_url,
                    "repository_identity_verified": False,
                }
            )

    def inspect(
        self,
        view: RepositoryView,
        *,
        url: str,
        commit: str,
        default_branch: str | None,
        ref: str,
    ) -> RepositoryInfo:
        paths = view.paths
        if not any(path.endswith(".py") or path == "pyproject.toml" for path in paths):
            raise RepositoryError("no supported Python project was detected", stage="analyze")
        evidence: list[EvidenceItem] = []
        entries: list[EvidenceItem] = []
        warnings: list[str] = []
        versions: list[str] = []
        dependencies: list[str] = []
        pytest_detected = False
        dependency_files = tuple(
            path
            for path in paths
            if (
                PurePosixPath(path).name in {"pyproject.toml", "poetry.lock"}
                or re.fullmatch(r"(?:requirements/[^/]+|requirements[^/]*)\.txt", path)
            )
        )

        def fact(value: str, source: str) -> EvidenceItem:
            return EvidenceItem(value=value, origin=EvidenceOrigin.STATIC_FACT, source=source)

        for path in dependency_files:
            evidence.append(fact("dependency manifest present", path))
            try:
                text = view.read(path).decode("utf-8-sig")
                if path.endswith(".txt"):
                    dependencies.extend(text.splitlines())
                elif path == "pyproject.toml":
                    document = tomllib.loads(text)
                    project = _table(document.get("project"))
                    requirement = project.get("requires-python")
                    if isinstance(requirement, str):
                        versions.append(requirement)
                        evidence.append(fact(requirement, f"{path}:project.requires-python"))
                    raw_dependencies = project.get("dependencies", [])
                    if isinstance(raw_dependencies, list):
                        dependencies.extend(str(item) for item in raw_dependencies)
                    for values in _table(project.get("optional-dependencies")).values():
                        if isinstance(values, list):
                            dependencies.extend(str(item) for item in values)
                    for group in ("scripts", "gui-scripts"):
                        for name, target in _table(project.get(group)).items():
                            if isinstance(target, str):
                                entries.append(
                                    fact(f"{name} = {target}", f"{path}:project.{group}")
                                )
                    tool = _table(document.get("tool"))
                    pytest_detected |= bool(_table(tool.get("pytest")))
                    poetry = _table(tool.get("poetry"))
                    poetry_dependencies = _table(poetry.get("dependencies"))
                    python = poetry_dependencies.get("python")
                    if isinstance(python, str):
                        versions.append(python)
                        evidence.append(fact(python, f"{path}:tool.poetry.dependencies.python"))
                    dependencies.extend(poetry_dependencies)
                    for name, target in _table(poetry.get("scripts")).items():
                        entries.append(fact(f"{name} = {target}", f"{path}:tool.poetry.scripts"))
                    for poetry_group in _table(poetry.get("group")).values():
                        dependencies.extend(_table(_table(poetry_group).get("dependencies")))
                elif path == "poetry.lock":
                    packages = tomllib.loads(text).get("package", [])
                    if isinstance(packages, list):
                        dependencies.extend(
                            str(_table(package).get("name", "")) for package in packages
                        )
            except (UnicodeError, tomllib.TOMLDecodeError, RepositoryError) as error:
                warnings.append(f"could not statically parse {path}: {type(error).__name__}")

        for path in (".python-version", "runtime.txt"):
            if path in paths:
                value = view.read(path).decode("utf-8", errors="replace").strip()
                if re.fullmatch(r"(?:python-)?[0-9][0-9a-zA-Z.+-]*(?:\s+[0-9][0-9.]*)*", value):
                    versions.append(value)
                    evidence.append(fact(value, path))
        if "setup.cfg" in paths:
            try:
                configuration = configparser.ConfigParser(interpolation=None)
                configuration.read_string(view.read("setup.cfg").decode("utf-8-sig"))
                if configuration.has_option("options", "python_requires"):
                    value = configuration.get("options", "python_requires")
                    versions.append(value)
                    evidence.append(fact(value, "setup.cfg:options.python_requires"))
                if configuration.has_section("options.entry_points"):
                    for group, value in configuration.items("options.entry_points"):
                        entries.append(fact(value, f"setup.cfg:options.entry_points.{group}"))
                pytest_detected |= configuration.has_section("tool:pytest")
                dependencies.append(configuration.get("options", "install_requires", fallback=""))
            except (configparser.Error, UnicodeError) as error:
                warnings.append(f"invalid setup.cfg: {type(error).__name__}")
        if "setup.py" in paths:
            try:
                tree = ast.parse(view.read("setup.py"), filename="setup.py")
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    if not (
                        (isinstance(node.func, ast.Name) and node.func.id == "setup")
                        or (isinstance(node.func, ast.Attribute) and node.func.attr == "setup")
                    ):
                        continue
                    for keyword in node.keywords:
                        if (
                            keyword.arg == "python_requires"
                            and isinstance(keyword.value, ast.Constant)
                            and isinstance(keyword.value.value, str)
                        ):
                            versions.append(keyword.value.value)
                            evidence.append(fact(keyword.value.value, "setup.py:python_requires"))
                warnings.append(
                    "setup.py was parsed as syntax only; dynamic metadata is unresolved"
                )
            except (SyntaxError, ValueError, RecursionError) as error:
                warnings.append(f"invalid setup.py syntax: {type(error).__name__}")
        tests = tuple(
            path
            for path in paths
            if path.endswith(".py")
            and (PurePosixPath(path).name.startswith("test_") or path.endswith("_test.py"))
        )
        test_directories = tuple(
            sorted(
                {
                    "/".join(parts[: index + 1])
                    for path in paths
                    for parts in [PurePosixPath(path).parts]
                    for index, part in enumerate(parts[:-1])
                    if part in {"tests", "test"}
                }
            )
        )
        pytest_detected |= "pytest.ini" in paths
        pytest_detected |= any(re.search(r"\bpytest\b", item) for item in dependencies)
        for path in tests[:20]:
            if b"import pytest" in view.read(path) or b"from pytest" in view.read(path):
                pytest_detected = True
                evidence.append(fact("pytest import", path))
                break
        for path in paths:
            name = PurePosixPath(path).name
            if name == "__main__.py":
                entries.append(fact(path, f"{path}:module-entry-file"))
            elif name in {"main.py", "app.py", "manage.py", "wsgi.py", "asgi.py"}:
                entries.append(
                    EvidenceItem(value=path, origin=EvidenceOrigin.HEURISTIC, source="entry-name")
                )
        managers = tuple(
            name
            for name, detected in (
                ("pip", bool(dependency_files) or "setup.py" in paths),
                ("poetry", "poetry.lock" in paths),
            )
            if detected
        )
        return RepositoryInfo(
            repository_url=repository_url(url),
            default_branch=default_branch,
            current_commit=commit,
            analyzed_ref=ref,
            files=paths,
            environment=EnvironmentInfo(
                python_requirements=tuple(dict.fromkeys(versions)),
                dependency_files=dependency_files,
                package_managers=managers,
                test_frameworks=("pytest",) if pytest_detected else (),
                evidence=tuple(evidence),
            ),
            test_directories=test_directories,
            test_files=tests,
            entry_points=tuple(entries),
            evidence=tuple(evidence),
            warnings=tuple(warnings),
        )
