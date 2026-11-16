"""Deterministic extraction of untrusted Issue data, never instructions to execute."""

import json
import re
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from repo_repro.core.context import (
    BugDescription,
    EnvironmentInfo,
    EvidenceItem,
    EvidenceOrigin,
    IssueContext,
    RepositoryInfo,
    ReproductionStep,
)
from repo_repro.core.exceptions import InputError
from repo_repro.core.models import ArtifactRef
from repo_repro.core.urls import parse_issue_url
from repo_repro.github.client import GitHubClient


class _Label(BaseModel):
    name: str


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    number: int = Field(ge=1)
    html_url: str
    title: str
    body: str | None = None
    labels: tuple[_Label | str, ...] = ()
    pull_request: dict[str, object] | None = None


def _sections(body: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {"description": []}
    heading = "description"
    fence: str | None = None
    for line in body.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = None if fence == marker else (marker if fence is None else fence)
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line) if fence is None else None
        if match:
            heading = match[1].strip().lower()
            sections.setdefault(heading, [])
        else:
            sections[heading].append(line)
    return {key: "\n".join(lines).strip() for key, lines in sections.items()}


def _section(sections: dict[str, str], words: tuple[str, ...]) -> str | None:
    selected = [value for key, value in sections.items() if any(word in key for word in words)]
    return "\n\n".join(value for value in selected if value) or None


def _evidence(value: str, origin: EvidenceOrigin, source: str) -> EvidenceItem:
    return EvidenceItem(value=value, origin=origin, source=source)


class IssueParser:
    def __init__(self, client: GitHubClient | None = None, *, max_bytes: int = 2_097_152) -> None:
        self.client = client or GitHubClient()
        self.max_bytes = max_bytes

    def parse(self, url: str | None = None, *, issue_json: Path | None = None) -> IssueContext:
        if issue_json is not None:
            try:
                with issue_json.open("rb") as stream:
                    content = stream.read(self.max_bytes + 1)
                if len(content) > self.max_bytes:
                    raise InputError("local Issue JSON exceeded the size limit")
                payload: object = json.loads(content)
            except (OSError, ValueError, RecursionError) as error:
                raise InputError("cannot read local Issue JSON") from error
            return self.parse_payload(payload, source="local_json", requested_url=url)
        if url is None:
            raise InputError("provide an Issue URL or local Issue JSON")
        return self.parse_payload(self.client.get_issue(url), source="github", requested_url=url)

    def parse_payload(
        self,
        payload: object,
        *,
        source: Literal["github", "local_json"] = "local_json",
        requested_url: str | None = None,
    ) -> IssueContext:
        try:
            raw = _Payload.model_validate(payload)
        except ValidationError as error:
            raise InputError("Issue JSON does not match the GitHub Issue schema") from error
        identity = parse_issue_url(raw.html_url)
        if identity.number != raw.number or (
            requested_url is not None and parse_issue_url(requested_url) != identity
        ):
            raise InputError("Issue JSON identity does not match the requested Issue")
        if raw.pull_request is not None:
            raise InputError("pull requests are not supported by the Issue parser")
        body = raw.body or ""
        if len(body.encode("utf-8")) > self.max_bytes:
            raise InputError("Issue body exceeded the size limit")
        sections = _sections(body)
        expected = _section(sections, ("expected", "预期", "期望"))
        actual = _section(sections, ("actual", "observed", "实际", "实际结果"))
        reproduction = _section(sections, ("reproduc", "repro steps", "复现", "重现"))
        steps: list[ReproductionStep] = []
        if reproduction:
            lines = re.findall(r"^\s*(?:\d+[.)]|[-*])\s+(.+)$", reproduction, re.MULTILINE)
            for index, text in enumerate(lines or [reproduction], start=1):
                steps.append(ReproductionStep(order=index, description=text))
        environment = _section(sections, ("environment", "versions", "system info", "环境"))
        claims = tuple(
            _evidence(value, EvidenceOrigin.REPORTER_STATEMENT, f"issue.body:{key}")
            for key, value in sections.items()
            if value
        )
        speculation = tuple(
            _evidence(line.strip(), EvidenceOrigin.REPORTER_SPECULATION, "issue.body")
            for line in body.splitlines()
            if re.search(r"\b(?:suspect|perhaps|guess|I think)\b|怀疑|猜测|可能", line, re.I)
        )
        logs = tuple(
            _evidence(block, EvidenceOrigin.REPORTER_STATEMENT, "issue.body:fenced-block")
            for _, block in re.findall(r"```([^\n]*)\n(.*?)```", body, re.S)
            if re.search(r"Traceback|\b\w*(?:Error|Exception)\b|^E\s+", block, re.M)
        )
        log_section = _section(sections, ("error log", "traceback", "错误日志", "报错日志"))
        extra_logs = [log_section] if log_section else []
        extra_logs.extend(
            block
            for block in re.findall(r"(?m)(?:^(?: {4}|\t).*(?:\n|$))+", body)
            if re.search(r"Traceback|\b\w*(?:Error|Exception)\b", block)
        )
        logs += tuple(
            _evidence(value, EvidenceOrigin.REPORTER_STATEMENT, "issue.body:error-log")
            for value in dict.fromkeys(extra_logs)
            if value not in {item.value for item in logs}
        )
        paths: list[EvidenceItem] = []
        rejected: list[str] = []
        candidates = re.findall(r'File ["\']([^"\']+\.py)["\']', body)
        candidates += re.findall(r"(?<![\w./\\:-])([\w./\\-]+\.py)(?!\w|\.\w)", body)
        for candidate in dict.fromkeys(candidates):
            try:
                ArtifactRef.validate_path(candidate)
            except ValueError:
                rejected.append(candidate)
            else:
                paths.append(_evidence(candidate, EvidenceOrigin.REPORTER_STATEMENT, "issue.body"))
        labels = tuple(label if isinstance(label, str) else label.name for label in raw.labels)
        return IssueContext(
            issue_url=identity.url,
            repository_url=identity.repository_url,
            number=raw.number,
            title=raw.title,
            body=body,
            labels=labels,
            source=source,
            bug=BugDescription(
                summary=raw.title,
                expected_behavior=expected,
                actual_behavior=actual,
                reproduction_steps=tuple(steps),
                error_logs=logs,
            ),
            environment=EnvironmentInfo(
                reported_details=(
                    (
                        _evidence(
                            environment, EvidenceOrigin.REPORTER_STATEMENT, "issue.body:environment"
                        ),
                    )
                    if environment
                    else ()
                )
            ),
            api_facts=tuple(
                _evidence(value, EvidenceOrigin.API_FACT, key)
                for key, value in (
                    ("issue.title", raw.title),
                    ("issue.number", str(raw.number)),
                    ("issue.labels", ", ".join(labels)),
                )
            ),
            reporter_statements=claims,
            reporter_speculations=speculation,
            related_code_files=tuple(paths),
            rejected_paths=tuple(rejected),
        )

    def relate(self, context: IssueContext, repository: RepositoryInfo) -> IssueContext:
        if repository.repository_url != context.repository_url:
            raise InputError("Issue and repository identities differ")
        known = {item.value for item in context.related_code_files}
        basenames = {PurePosixPath(path.replace("\\", "/")).name for path in context.rejected_paths}
        related = (
            context.related_code_files
            + tuple(
                _evidence(path, EvidenceOrigin.HEURISTIC, "repository:traceback-basename-match")
                for path in repository.files
                if path not in known and PurePosixPath(path).name in basenames
            )[:20]
        )
        stems = {PurePosixPath(item.value).stem for item in related}
        suggestions = tuple(
            _evidence(path, EvidenceOrigin.HEURISTIC, "repository:test-name-match")
            for path in repository.test_files
            if any(PurePosixPath(path).stem in {f"test_{stem}", f"{stem}_test"} for stem in stems)
        )
        return context.model_copy(
            update={
                "repository": repository,
                "related_code_files": related,
                "possible_test_files": suggestions,
            }
        )
