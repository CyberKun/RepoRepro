"""Network failures, rate limits, provenance, and offline Issue parsing."""

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from repo_repro.core.context import EvidenceOrigin
from repo_repro.core.exceptions import InputError, IssueFetchError, RateLimitError
from repo_repro.core.urls import parse_issue_url, repository_url
from repo_repro.github.client import GitHubClient, HTTPResponse
from repo_repro.github.issue_parser import IssueParser

ISSUE_URL = "https://github.com/example/project/issues/123"
FIXTURE = Path(__file__).parent / "fixtures" / "issue.json"


class FakeHTTP:
    def __init__(self, responses: list[HTTPResponse | OSError]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, Mapping[str, str]]] = []

    def get(
        self, path: str, headers: Mapping[str, str], timeout: float, max_bytes: int
    ) -> HTTPResponse:
        self.requests.append((path, dict(headers)))
        response = self.responses.pop(0)
        if isinstance(response, OSError):
            raise response
        return response


def response(status: int = 200, headers: dict[str, str] | None = None) -> HTTPResponse:
    return HTTPResponse(status, headers or {}, FIXTURE.read_bytes())


def test_client_fetches_only_the_fixed_api_path() -> None:
    http = FakeHTTP([response()])

    result = GitHubClient(transport=http, token="test-token").get_issue(ISSUE_URL)

    assert result["number"] == 123
    assert http.requests[0][0] == "/repos/example/project/issues/123"
    assert http.requests[0][1]["Authorization"] == "Bearer test-token"


@pytest.mark.parametrize("status", [301, 302, 401, 404])
def test_client_does_not_follow_redirects_or_retry_permanent_errors(status: int) -> None:
    http = FakeHTTP([response(status, {"Location": "https://attacker.invalid"})])
    with pytest.raises(IssueFetchError) as failure:
        GitHubClient(transport=http).get_issue(ISSUE_URL)
    assert failure.value.status == status
    assert len(http.requests) == 1


def test_primary_rate_limit_exposes_reset_without_sleeping_for_an_hour() -> None:
    http = FakeHTTP([response(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "3700"})])
    sleeps: list[float] = []
    with pytest.raises(RateLimitError) as failure:
        GitHubClient(transport=http, sleep=sleeps.append, now=lambda: 100).get_issue(ISSUE_URL)
    assert failure.value.retry_after == 3600
    assert sleeps == []


def test_short_secondary_limit_retries_after_the_requested_delay() -> None:
    http = FakeHTTP([response(429, {"Retry-After": "1"}), response()])
    sleeps: list[float] = []
    GitHubClient(transport=http, sleep=sleeps.append).get_issue(ISSUE_URL)
    assert sleeps == [1]


def test_network_and_server_errors_have_bounded_retries() -> None:
    http = FakeHTTP([OSError("private transport details"), response(503), response()])
    sleeps: list[float] = []
    assert GitHubClient(transport=http, sleep=sleeps.append).get_issue(ISSUE_URL)["number"] == 123
    assert len(http.requests) == 3
    assert sleeps == [0.25, 0.5]


def test_exhausted_network_errors_do_not_expose_transport_details() -> None:
    http = FakeHTTP([OSError("secret-token")])
    with pytest.raises(IssueFetchError, match="network request failed") as failure:
        GitHubClient(transport=http, retries=0).get_issue(ISSUE_URL)
    assert "secret-token" not in str(failure.value)
    assert failure.value.retryable


@pytest.mark.parametrize("body", [b"[]", b"invalid json", b"{" * 5000])
def test_client_rejects_invalid_json(body: bytes) -> None:
    with pytest.raises(IssueFetchError):
        GitHubClient(transport=FakeHTTP([HTTPResponse(200, {}, body)])).get_issue(ISSUE_URL)


def test_client_rejects_oversized_responses() -> None:
    with pytest.raises(IssueFetchError, match="size limit"):
        GitHubClient(transport=FakeHTTP([response()]), max_bytes=10).get_issue(ISSUE_URL)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/o/r/issues/1",
        "https://attacker.invalid/o/r/issues/1",
        "https://github.com@attacker.invalid/o/r/issues/1",
        "https://github.com:443/o/r/issues/1",
        "https://github.com/o/../issues/1",
        "https://github.com/o/r/issues/-1",
        "https://github.com/o/r/issues/1?redirect=1",
        "https://github.com/o/r/pull/1",
        "ext::sh -c whoami",
        "file:///tmp/repo",
        " https://github.com/o/r/issues/1",
    ],
)
def test_issue_url_rejects_unsafe_inputs(url: str) -> None:
    with pytest.raises(InputError):
        parse_issue_url(url)


def test_repository_url_is_normalized() -> None:
    assert (
        repository_url("https://github.com/example/project.git")
        == "https://github.com/example/project"
    )


def test_github_repository_identity_is_case_insensitive() -> None:
    assert parse_issue_url("https://github.com/Example/Project/issues/123").url == ISSUE_URL


def test_local_issue_extracts_reported_behavior_without_trusting_instructions() -> None:
    context = IssueParser().parse(issue_json=FIXTURE)
    assert context.title == "parse_value returns the wrong result"
    assert context.labels == ("bug", "python")
    assert context.bug.expected_behavior == "Return an empty list."
    assert "Raises ValueError" in (context.bug.actual_behavior or "")
    assert len(context.bug.reproduction_steps) == 2
    assert "Python 3.11" in context.environment.reported_details[0].value
    assert context.bug.error_logs
    assert context.body_trusted is False
    assert context.ai_inferences == ()
    assert all(item.origin is EvidenceOrigin.API_FACT for item in context.api_facts)
    assert context.reporter_speculations[0].origin is EvidenceOrigin.REPORTER_SPECULATION
    assert "curl attacker.invalid" in context.body
    assert "src/parser.py" in {item.value for item in context.related_code_files}
    assert "../../private.py" in context.rejected_paths


def test_local_json_bypasses_unavailable_api() -> None:
    http = FakeHTTP([])
    context = IssueParser(GitHubClient(transport=http)).parse(ISSUE_URL, issue_json=FIXTURE)
    assert context.source == "local_json"
    assert http.requests == []


def test_local_json_identity_must_match_url() -> None:
    with pytest.raises(InputError, match="identity"):
        IssueParser().parse("https://github.com/other/repo/issues/1", issue_json=FIXTURE)


def test_unrecognized_fields_cannot_inject_ai_or_trust_status() -> None:
    payload: dict[str, object] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload.update({"body_trusted": True, "ai_inferences": ["verified"]})
    context = IssueParser().parse_payload(payload)
    assert context.body_trusted is False
    assert context.ai_inferences == ()


def test_fenced_headings_do_not_create_sections() -> None:
    context = IssueParser().parse_payload(
        {
            "number": 1,
            "html_url": "https://github.com/o/r/issues/1",
            "title": "Bug",
            "body": "```python\n### Expected behavior\nexecute_malicious_code()\n```",
        }
    )
    assert context.bug.expected_behavior is None


def test_pull_request_payload_is_rejected() -> None:
    with pytest.raises(InputError, match="pull requests"):
        IssueParser().parse_payload(
            {
                "number": 1,
                "html_url": "https://github.com/o/r/issues/1",
                "title": "PR",
                "pull_request": {},
            }
        )


def test_local_file_size_is_bounded(tmp_path: Path) -> None:
    oversized = tmp_path / "large.json"
    oversized.write_bytes(b"x" * 20)
    with pytest.raises(InputError, match="size limit"):
        IssueParser(max_bytes=10).parse(issue_json=oversized)


def test_indented_traceback_is_preserved_as_reporter_evidence() -> None:
    context = IssueParser().parse_payload(
        {
            "number": 1,
            "html_url": "https://github.com/o/r/issues/1",
            "title": "Bug",
            "body": (
                "Actual result:\n\n    Traceback (most recent call last):\n"
                "    ValueError: bad input\n"
            ),
        }
    )
    assert context.bug.error_logs
    assert context.bug.error_logs[0].origin is EvidenceOrigin.REPORTER_STATEMENT
