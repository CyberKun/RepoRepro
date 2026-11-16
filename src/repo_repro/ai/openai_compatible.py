"""Bounded Chat Completions transport; credentials never enter audit artifacts."""

import contextlib
import http.client
import ipaddress
import json
import math
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import SecretStr

from repo_repro.ai.base import ModelReply, ProviderError
from repo_repro.storage.artifacts import FileArtifactStore


@dataclass(frozen=True)
class ModelHTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class ModelHTTPTransport(Protocol):
    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        *,
        timeout: float,
        max_bytes: int,
    ) -> ModelHTTPResponse: ...


class HTTPSModelTransport:
    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        *,
        timeout: float,
        max_bytes: int,
    ) -> ModelHTTPResponse:
        parts = urlsplit(url)
        connection: http.client.HTTPConnection
        if parts.scheme == "https":
            connection = http.client.HTTPSConnection(
                parts.hostname or "",
                parts.port,
                timeout=timeout,
                context=ssl.create_default_context(),
            )
        else:
            connection = http.client.HTTPConnection(
                parts.hostname or "", parts.port, timeout=timeout
            )
        deadline = time.monotonic() + timeout
        try:
            connection.request("POST", parts.path, body=body, headers=dict(headers))
            response = connection.getresponse()
            chunks: list[bytes] = []
            count = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("model response deadline exceeded")
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(65_536, max_bytes + 1 - count))
                if not chunk:
                    break
                chunks.append(chunk)
                count += len(chunk)
                if count > max_bytes:
                    raise ValueError("model response exceeded size limit")
            return ModelHTTPResponse(response.status, dict(response.getheaders()), b"".join(chunks))
        finally:
            connection.close()


@dataclass(frozen=True)
class OpenAIConfig:
    model: str
    api_key: SecretStr | None = field(default=None, repr=False)
    base_url: str = "https://api.openai.com/v1"
    timeout: float = 60
    max_completion_tokens: int = 4096
    token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_completion_tokens"
    allow_local_http: bool = False

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid model endpoint")
        if any(ord(char) < 33 for char in self.base_url):
            raise ValueError("invalid model endpoint")
        if parsed.scheme != "https":
            try:
                local = (
                    parsed.hostname == "localhost"
                    or ipaddress.ip_address(parsed.hostname).is_loopback
                )
            except ValueError:
                local = False
            if parsed.scheme != "http" or not self.allow_local_http or not local:
                raise ValueError("HTTP is allowed only for explicitly enabled loopback providers")
        if not self.model.strip() or not math.isfinite(self.timeout) or not 0 < self.timeout <= 120:
            raise ValueError("invalid model or timeout")
        if not 1 <= self.max_completion_tokens <= 16_384:
            raise ValueError("invalid model output budget")
        if self.api_key and any(ord(char) < 33 for char in self.api_key.get_secret_value()):
            raise ValueError("invalid API credential")


class OpenAICompatibleProvider:
    def __init__(
        self,
        config: OpenAIConfig,
        artifacts: FileArtifactStore,
        *,
        transport: ModelHTTPTransport | None = None,
    ) -> None:
        self.config = config
        self.artifacts = artifacts
        self.transport = transport or HTTPSModelTransport()

    def redact(self, text: str) -> str:
        secret = self.config.api_key.get_secret_value() if self.config.api_key else ""
        return text.replace(secret, "[REDACTED]") if secret else text

    def _sanitize(self, value: object) -> object:
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {self.redact(str(key)): self._sanitize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._sanitize(item) for item in value]
        return value

    def complete(self, system: str, user: str) -> ModelReply:
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        request: dict[str, object] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": self.redact(system)},
                {"role": "user", "content": self.redact(user)},
            ],
            "response_format": {"type": "json_object"},
            self.config.token_parameter: self.config.max_completion_tokens,
        }
        payload = json.dumps(request, ensure_ascii=True).encode()
        if len(payload) > 524_288:
            raise ValueError("model request exceeded context size limit")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = "Bearer " + self.config.api_key.get_secret_value()
        started = time.monotonic()
        audit: dict[str, object] = {
            "schema_version": "1.0",
            "id": str(uuid4()),
            "provider": "openai-compatible",
            "endpoint": url,
            "method": "POST",
            "request": request,
            "request_headers": {
                key: "[REDACTED]" if key == "Authorization" else value
                for key, value in headers.items()
            },
            "started_at": datetime.now(UTC).isoformat(),
            "attempt": 1,
        }
        response_body = ""
        failure: str | None = None
        try:
            response = self.transport.post(
                url, headers, payload, timeout=self.config.timeout, max_bytes=1_048_576
            )
            if len(response.body) > 1_048_576:
                raise ValueError("oversized model response")
            response_body = self.redact(response.body.decode("utf-8"))
            with contextlib.suppress(ValueError, RecursionError):
                response_body = json.dumps(self._sanitize(json.loads(response_body)))
            audit.update(
                {
                    "http_status": response.status,
                    "response_body": response_body,
                    "response_headers": {
                        key: self.redact(value)
                        for key, value in response.headers.items()
                        if key.lower() not in {"authorization", "set-cookie", "proxy-authorization"}
                    },
                }
            )
            if response.status != 200:
                failure = f"model endpoint returned HTTP {response.status}"
        except Exception as error:
            failure = f"model transport failed ({type(error).__name__})"
        audit.update(
            {
                "finished_at": datetime.now(UTC).isoformat(),
                "duration_seconds": time.monotonic() - started,
                "error": failure,
            }
        )
        reference = self.artifacts.put_bytes(
            json.dumps(self._sanitize(audit)).encode(), media_type="application/json"
        )
        if failure:
            raise ProviderError(failure, reference) from None
        try:
            envelope = json.loads(response_body)
            choice = envelope["choices"][0]
            if (
                choice["finish_reason"] != "stop"
                or choice["message"].get("refusal")
                or choice["message"].get("tool_calls")
            ):
                raise ValueError("unfinished, refused or tool-based completion")
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content:
                raise ValueError("missing model content")
            return ModelReply(content, reference)
        except (ValueError, KeyError, IndexError, TypeError, RecursionError):
            raise ProviderError(
                "invalid, truncated or refused model completion", reference
            ) from None
