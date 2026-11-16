"""Application errors retain structured context without changing their message."""

from repo_repro.core.exceptions import ConfigurationError, PolicyViolation, RepoReproError


def test_application_error_preserves_context() -> None:
    error = RepoReproError("storage is unavailable", stage="storage", retryable=True)

    assert str(error) == "storage is unavailable"
    assert error.message == "storage is unavailable"
    assert error.stage == "storage"
    assert error.retryable is True


def test_error_subclasses_have_stable_distinct_codes() -> None:
    configuration = ConfigurationError("invalid setting")
    policy = PolicyViolation("network access denied", stage="policy")

    assert isinstance(configuration, RepoReproError)
    assert isinstance(policy, RepoReproError)
    assert configuration.code == "INVALID_CONFIGURATION"
    assert policy.code == "POLICY_DENIED"
    assert configuration.retryable is False
