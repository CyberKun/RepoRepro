"""Typed application errors, distinct from observed test failures."""

from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from repo_repro.core.models import ExecutionResult


class RepoReproError(Exception):
    """Base application failure with a stable code and safe display message."""

    code: ClassVar[str] = "REPO_REPRO_ERROR"

    def __init__(self, message: str, *, stage: str = "bootstrap", retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.retryable = retryable


class ConfigurationError(RepoReproError):
    """A local configuration could not be accepted."""

    code: ClassVar[str] = "INVALID_CONFIGURATION"


class PolicyViolation(RepoReproError):
    """An action was rejected by execution policy."""

    code: ClassVar[str] = "POLICY_DENIED"


class InputError(RepoReproError):
    """An untrusted input does not meet the accepted format."""

    code: ClassVar[str] = "INVALID_INPUT"


class IssueFetchError(RepoReproError):
    """A GitHub request failed without exposing transport credentials."""

    code: ClassVar[str] = "ISSUE_FETCH_FAILED"

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message, stage="github", retryable=retryable)
        self.status = status
        self.retry_after = retry_after


class RateLimitError(IssueFetchError):
    """GitHub requires the caller to wait before another request."""

    code: ClassVar[str] = "GITHUB_RATE_LIMITED"


class RepositoryError(RepoReproError):
    """Repository acquisition or static inspection failed."""

    code: ClassVar[str] = "REPOSITORY_ERROR"


class SandboxError(RepoReproError):
    """Sandbox infrastructure could not satisfy the requested operation."""

    code: ClassVar[str] = "SANDBOX_UNAVAILABLE"


class ExecutionBackendError(SandboxError):
    """An infrastructure failure with an already persisted attempted execution."""

    def __init__(self, message: str, result: "ExecutionResult", *, stage: str) -> None:
        super().__init__(message, stage=stage)
        self.result = result
