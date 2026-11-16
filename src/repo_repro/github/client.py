"""Bounded GitHub HTTPS requests with injectable transport and conservative retries."""

import http.client
import json
import math
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Protocol

from repo_repro.core.exceptions import InputError, IssueFetchError, RateLimitError
from repo_repro.core.urls import parse_issue_url


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HTTPTransport(Protocol):
    def get(
        self, path: str, headers: Mapping[str, str], timeout: float, max_bytes: int
    ) -> HTTPResponse: ...


class GitHubHTTPTransport:
    """Never follows redirects or forwards credentials to another host."""

    def get(
        self, path: str, headers: Mapping[str, str], timeout: float, max_bytes: int
    ) -> HTTPResponse:
        connection = http.client.HTTPSConnection(
            "api.github.com", timeout=timeout, context=ssl.create_default_context()
        )
        deadline = time.monotonic() + timeout
        try:
            connection.request("GET", path, headers=dict(headers))
            response = connection.getresponse()
            chunks: list[bytes] = []
            size = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("GitHub response deadline exceeded")
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(65_536, max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > max_bytes:
                    raise IssueFetchError("GitHub response exceeded the size limit")
            return HTTPResponse(response.status, dict(response.getheaders()), b"".join(chunks))
        finally:
            connection.close()


class GitHubClient:
    def __init__(
        self,
        *,
        token: str | None = None,
        transport: HTTPTransport | None = None,
        timeout: float = 15,
        max_bytes: int = 2_097_152,
        retries: int = 2,
        max_retry_delay: float = 2,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        if timeout <= 0 or max_bytes < 1 or not 0 <= retries <= 3 or max_retry_delay < 0:
            raise ValueError("invalid HTTP limits")
        if token and any(ord(char) < 32 for char in token):
            raise InputError("invalid GitHub token")
        self._transport = transport or GitHubHTTPTransport()
        self._headers = {"Accept": "application/vnd.github+json", "User-Agent": "repo-repro/0.1"}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.retries = retries
        self.max_retry_delay = max_retry_delay
        self._sleep = sleep
        self._now = now

    def get_issue(self, url: str) -> dict[str, object]:
        path = parse_issue_url(url).api_path
        for attempt in range(self.retries + 1):
            try:
                response = self._transport.get(path, self._headers, self.timeout, self.max_bytes)
            except (OSError, http.client.HTTPException) as error:
                if attempt < self.retries:
                    self._sleep(min(0.25 * 2**attempt, self.max_retry_delay))
                    continue
                raise IssueFetchError("GitHub network request failed", retryable=True) from error
            if len(response.body) > self.max_bytes:
                raise IssueFetchError("GitHub response exceeded the size limit")
            headers = {key.lower(): value for key, value in response.headers.items()}
            limited = response.status == 429 or (
                response.status == 403
                and (
                    headers.get("x-ratelimit-remaining") == "0"
                    or "retry-after" in headers
                    or b"rate limit" in response.body.lower()
                )
            )
            if limited:
                delay = self._retry_delay(headers)
                if delay is not None and delay <= self.max_retry_delay and attempt < self.retries:
                    self._sleep(delay)
                    continue
                raise RateLimitError(
                    "GitHub rate limit reached; use local Issue JSON or retry later",
                    status=response.status,
                    retry_after=delay,
                    retryable=True,
                )
            if response.status in {500, 502, 503, 504} and attempt < self.retries:
                self._sleep(min(0.25 * 2**attempt, self.max_retry_delay))
                continue
            if response.status != 200:
                raise IssueFetchError(
                    f"GitHub API returned HTTP {response.status}; local Issue JSON is supported",
                    status=response.status,
                    retryable=response.status >= 500,
                )
            try:
                result: object = json.loads(response.body)
            except (ValueError, UnicodeDecodeError, RecursionError) as error:
                raise IssueFetchError("GitHub returned invalid JSON") from error
            if not isinstance(result, dict) or any(not isinstance(key, str) for key in result):
                raise IssueFetchError("GitHub response must be a JSON object")
            return dict(result)
        raise AssertionError("bounded request loop did not return")

    def _retry_delay(self, headers: Mapping[str, str]) -> float | None:
        value = headers.get("retry-after")
        try:
            if value is not None:
                try:
                    numeric = float(value)
                    return max(0, numeric) if math.isfinite(numeric) else None
                except ValueError:
                    return max(0, parsedate_to_datetime(value).timestamp() - self._now())
            reset = headers.get("x-ratelimit-reset")
            return max(0, float(reset) - self._now()) if reset else None
        except (ValueError, TypeError, OverflowError):
            return None
