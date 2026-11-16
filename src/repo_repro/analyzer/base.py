"""Read-only analysis ports; adapters have no execution capability."""

from typing import Protocol

from repo_repro.core.context import RepositoryInfo


class RepositoryView(Protocol):
    @property
    def paths(self) -> tuple[str, ...]: ...

    def read(self, path: str) -> bytes: ...


class RepositoryAnalyzer(Protocol):
    def inspect(
        self,
        view: RepositoryView,
        *,
        url: str,
        commit: str,
        default_branch: str | None,
        ref: str,
    ) -> RepositoryInfo: ...
