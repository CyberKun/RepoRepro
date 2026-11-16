"""Compatible wire contract, credential redaction and model validation."""

import json
import threading
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from repo_repro.ai.base import ProviderError
from repo_repro.ai.openai_compatible import (
    ModelHTTPResponse,
    OpenAICompatibleProvider,
    OpenAIConfig,
)
from repo_repro.core.exceptions import PolicyViolation
from repo_repro.core.models import ArtifactRef
from repo_repro.reproduction.generator import TestGenerator
from repo_repro.storage.artifacts import FileArtifactStore
from tests.repro_support import context, proposal

SECRET = "sk-self-owned-fake-secret"


class FakeTransport:
    def __init__(self, content: str, status: int = 200, finish_reason: str = "stop") -> None:
        self.response = ModelHTTPResponse(
            status,
            {"x-request-id": "request-123"},
            json.dumps(
                {
                    "id": "completion-123",
                    "model": "fixture-model",
                    "usage": {"total_tokens": 20},
                    "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
                }
            ).encode(),
        )
        self.requests: list[dict[str, object]] = []

    def post(
        self, url: str, headers: Mapping[str, str], body: bytes, *, timeout: float, max_bytes: int
    ) -> ModelHTTPResponse:
        assert headers["Authorization"] == "Bearer " + SECRET
        self.requests.append(json.loads(body))
        return self.response


def provider(
    root: Path, content: str, *, status: int = 200, finish_reason: str = "stop"
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        OpenAIConfig(model="fixture-model", api_key=SecretStr(SECRET)),
        FileArtifactStore(root),
        transport=FakeTransport(content, status, finish_reason),
    )


def test_metadata_and_evidence_are_saved_without_credentials(tmp_path: Path) -> None:
    client = provider(tmp_path, proposal().model_dump_json())
    generated = TestGenerator(client.artifacts, client).generate(context())
    assert generated.oracle == context().oracle
    record = json.loads(client.artifacts.read_bytes(generated.generation_record))
    audit = json.loads(
        client.artifacts.read_bytes(ArtifactRef.model_validate(record["model_audit"]))
    )
    assert audit["response_headers"]["x-request-id"] == "request-123"
    assert audit["request"]["response_format"] == {"type": "json_object"}
    records = [path.read_bytes() for path in (tmp_path / "objects").iterdir()]
    assert all(SECRET.encode() not in item for item in records)
    assert any(b"total_tokens" in item and b"messages" in item for item in records)


@pytest.mark.parametrize("content", ["{}", '{"verdict":"REPRODUCED"}', "not JSON"])
def test_model_outputs_cannot_skip_the_schema(tmp_path: Path, content: str) -> None:
    client = provider(tmp_path, content)
    with pytest.raises(ValidationError):
        TestGenerator(client.artifacts, client).generate(context())
    assert any(b'"rejected"' in path.read_bytes() for path in (tmp_path / "objects").iterdir())


@pytest.mark.parametrize("status", [302, 401, 429, 503])
def test_http_errors_are_audited_without_false_success(tmp_path: Path, status: int) -> None:
    client = provider(tmp_path, SECRET, status=status)
    with pytest.raises(ProviderError) as failure:
        TestGenerator(client.artifacts, client).generate(context())
    assert SECRET not in str(failure.value)
    assert all(
        SECRET.encode() not in path.read_bytes() for path in (tmp_path / "objects").iterdir()
    )


def test_truncated_completion_is_rejected(tmp_path: Path) -> None:
    client = provider(tmp_path, proposal().model_dump_json(), finish_reason="length")
    with pytest.raises(ProviderError):
        TestGenerator(client.artifacts, client).generate(context())


def test_model_cannot_generate_commands(tmp_path: Path) -> None:
    unsafe = proposal().model_copy(
        update={"test_code": "import os\ndef test_repro():\n assert os.system('id') == 0"}
    )
    client = provider(tmp_path, unsafe.model_dump_json())
    with pytest.raises(PolicyViolation):
        TestGenerator(client.artifacts, client).generate(context())


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/v1",
        "https://u:p@example.com/v1",
        "file:///tmp/model",
        "https://api.openai.com/v1?api_key=x",
    ],
)
def test_model_endpoints_require_explicit_safe_configuration(url: str) -> None:
    with pytest.raises(ValueError):
        OpenAIConfig(model="fixture", base_url=url)


def test_explicit_local_server_is_supported_without_api_key() -> None:
    config = OpenAIConfig(model="local", base_url="http://127.0.0.1:1234/v1", allow_local_http=True)
    assert config.api_key is None


def test_actual_http_compatible_transport_on_loopback(tmp_path: Path) -> None:
    received: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            assert self.path == "/v1/chat/completions"
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            payload = FakeTransport(proposal().model_dump_json()).response.body
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("x-request-id", "local-http-test")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        client = OpenAICompatibleProvider(
            OpenAIConfig(
                model="local",
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                allow_local_http=True,
            ),
            FileArtifactStore(tmp_path),
        )
        generated = TestGenerator(client.artifacts, client).generate(context())
        assert generated.code == proposal().test_code
        assert received[0]["model"] == "local"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
