"""Strict GitHub identifiers: never accept arbitrary transport URLs or commands."""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from repo_repro.core.exceptions import InputError


@dataclass(frozen=True)
class GitHubIssueURL:
    owner: str
    repository: str
    number: int

    @property
    def url(self) -> str:
        return f"{self.repository_url}/issues/{self.number}"

    @property
    def repository_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repository}"

    @property
    def api_path(self) -> str:
        return f"/repos/{self.owner}/{self.repository}/issues/{self.number}"


def _parts(value: str) -> list[str]:
    if value != value.strip() or any(ord(char) < 32 for char in value):
        raise InputError("GitHub URL contains whitespace or control characters")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise InputError("invalid GitHub URL") from error
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or parsed.query
        or parsed.fragment
    ):
        raise InputError("only canonical https://github.com URLs are supported")
    parts = parsed.path.removesuffix("/").split("/")[1:]
    if len(parts) < 2 or not re.fullmatch(r"[A-Za-z0-9-]+", parts[0]):
        raise InputError("invalid GitHub owner")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", parts[1]) or parts[1] in {".", ".."}:
        raise InputError("invalid GitHub repository")
    return parts


def parse_issue_url(value: str) -> GitHubIssueURL:
    parts = _parts(value)
    if len(parts) != 4 or parts[2] != "issues" or not re.fullmatch(r"[1-9][0-9]*", parts[3]):
        raise InputError("expected https://github.com/owner/repo/issues/123")
    return GitHubIssueURL(parts[0].lower(), parts[1].lower(), int(parts[3]))


def repository_url(value: str) -> str:
    parts = _parts(value)
    if len(parts) != 2:
        raise InputError("expected a GitHub repository URL")
    name = parts[1].removesuffix(".git")
    if not name or name in {".", ".."}:
        raise InputError("invalid GitHub repository")
    return f"https://github.com/{parts[0].lower()}/{name.lower()}"
