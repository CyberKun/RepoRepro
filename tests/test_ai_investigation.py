"""Bounded model decisions must not replace sealed contracts or independent evidence."""

import json
from pathlib import Path

import pytest
from git import Repo
from pydantic import ValidationError
from typer.testing import CliRunner

from repo_repro.ai.base import ModelReply, ProviderError
from repo_repro.cli import app
from repo_repro.core.exceptions import ExecutionBackendError, InputError, PolicyViolation
from repo_repro.core.models import ExecutionResult
from repo_repro.reproduction import cli as commands
from repo_repro.reproduction.ai_planner import AIPlanner
from repo_repro.reproduction.executor import ReproductionExecutor
from repo_repro.reproduction.investigation_models import InvestigationPlan
from repo_repro.reproduction.investigator import AIInvestigator
from repo_repro.reproduction.models import (
    DifferentialStatus,
    EvidenceQuote,
    ExecutionEnvironment,
    PytestRunRequest,
    ReproductionStatus,
)
from repo_repro.storage.artifacts import FileArtifactStore
from tests.integration.test_reproduction import repository_pair
from tests.repro_support import CODE, context, execution, proposal
from tests.test_differential import IMAGE, ScriptedBackend

SEED = CODE.replace("parse_value('') == 0", "parse_value('abc') == 3")


def plan() -> InvestigationPlan:
    return InvestigationPlan(
        summary="Empty input violates the specified return value.",
        expected_behavior="returns zero",
        actual_behavior="returns -1",
        preconditions=("empty input",),
        hypothesis="the empty-input branch returns a sentinel",
        source_paths=("calculator.py",),
        test_paths=("tests/test_calculator.py",),
        evidence=(EvidenceQuote(source="issue.body", quote="returns zero"),),
    )


class SequenceProvider:
    def __init__(self, store: FileArtifactStore, responses: list[str]) -> None:
        self.store = store
        self.responses = responses
        self.requests: list[dict[str, object]] = []

    def redact(self, text: str) -> str:
        return text.replace("private-provider-key", "[REDACTED]")

    def complete(self, system: str, user: str) -> ModelReply:
        self.requests.append({"system": system, "user": json.loads(user)})
        content = self.responses[len(self.requests) - 1]
        audit = self.store.put_bytes(
            json.dumps({"kind": "mock_model", "content": content}).encode(),
            media_type="application/json",
        )
        if content == "provider_error":
            raise ProviderError("fixture model unavailable", audit)
        return ModelReply(content=content, audit=audit)


class FeedbackBackend(ScriptedBackend):
    def run_test(self, request: PytestRunRequest) -> ExecutionResult:
        result = super().run_test(request)
        return result.model_copy(
            update={
                "stdout": self.store.put_bytes(
                    b"password=opaque-fixture-secret", media_type="text/plain"
                ),
                "stderr": self.store.put_bytes(b"private-provider-key", media_type="text/plain"),
            }
        )


def environment() -> ExecutionEnvironment:
    return ExecutionEnvironment.model_validate(
        {"image": IMAGE, "policy": {"isolation": "reviewed_docker"}}
    )


def test_ai_plans_revises_from_real_feedback_records_and_then_freezes_test(tmp_path: Path) -> None:
    source = tmp_path / "source"
    original, fixed = repository_pair(source)
    store = FileArtifactStore(tmp_path / "evidence")
    provider = SequenceProvider(store, [plan().model_dump_json(), proposal().model_dump_json()])
    backend = FeedbackBackend(
        store, ["pass", "pass", "target", "target", "target", "target", "pass", "pass"]
    )
    result = AIInvestigator(provider, ReproductionExecutor(backend, store), store).run(
        context().issue,
        source,
        context().oracle,
        environment(),
        ref=original,
        initial_code=SEED,
        fixed_commit=fixed,
    )
    assert result.stop_reason == "reproduced" and result.model_calls == 2
    assert len(result.attempts) == 2 and result.attempts[0].result is not None
    assert result.attempts[0].result.status is ReproductionStatus.NOT_REPRODUCED
    assert result.verification is not None
    assert result.verification.status is DifferentialStatus.DIFFERENTIAL_VALIDATED
    assert len(backend.calls) == 8
    assert all(call.test_code == CODE for call in backend.calls[2:])
    repair = provider.requests[1]["user"]
    assert isinstance(repair, dict)
    assert repair["feedback"]["status"] == "NOT_REPRODUCED"
    assert repair["feedback"]["previous_test"] == SEED
    assert "opaque-fixture-secret" not in json.dumps(repair)
    assert "private-provider-key" not in json.dumps(repair)
    assert repair["context"]["oracle"] == context().oracle.model_dump()
    assert all(fixed not in json.dumps(request) for request in provider.requests)
    assert all(attempt.generation_record is not None for attempt in result.attempts)
    assert result.attempts[1].feedback_record is not None


@pytest.mark.parametrize(
    "invalid",
    [
        "invalid JSON",
        proposal()
        .model_copy(
            update={
                "test_code": (
                    "import os\nfrom calculator import parse_value\n"
                    "def test_repro():\n assert parse_value('') == 0\n"
                )
            }
        )
        .model_dump_json(),
    ],
)
def test_rejected_candidate_can_be_repaired_without_executing_it(
    tmp_path: Path, invalid: str
) -> None:
    source = tmp_path / "source"
    original, _ = repository_pair(source)
    store = FileArtifactStore(tmp_path / "evidence")
    provider = SequenceProvider(
        store, [plan().model_dump_json(), invalid, proposal().model_dump_json()]
    )
    backend = FeedbackBackend(store, ["target", "target"])
    result = AIInvestigator(provider, ReproductionExecutor(backend, store), store).run(
        context().issue,
        source,
        context().oracle,
        environment(),
        ref=original,
    )
    assert result.stop_reason == "reproduced" and result.model_calls == 3
    assert len(backend.calls) == 2
    assert result.attempts[0].rejection and result.attempts[0].result is None
    assert result.attempts[0].generation_record is not None
    feedback = provider.requests[2]["user"]
    assert isinstance(feedback, dict) and feedback["feedback"]["validation_error"]


def test_default_generation_path_uses_plan_and_has_no_hidden_retry(tmp_path: Path) -> None:
    source = tmp_path / "source"
    original, _ = repository_pair(source)
    store = FileArtifactStore(tmp_path / "evidence")
    provider = SequenceProvider(store, [plan().model_dump_json(), proposal().model_dump_json()])
    backend = FeedbackBackend(store, ["target", "target"])
    result = AIInvestigator(provider, ReproductionExecutor(backend, store), store).run(
        context().issue,
        source,
        context().oracle,
        environment(),
        ref=original,
    )
    assert result.model_calls == 2 and len(result.attempts) == 1
    assert result.selected_result is not None
    assert result.selected_result.generated_test.oracle == context().oracle
    request = provider.requests[1]["user"]
    assert isinstance(request, dict) and request["diagnosis"]["origin"] == "ai_inference"


@pytest.mark.parametrize(
    "script,stop",
    [
        (["environment", "environment"], "environment_error"),
        (["timeout", "timeout"], "timeout"),
        (["target", "pass"], "flaky"),
    ],
)
def test_infrastructure_timeout_and_flaky_results_stop_model_repair(
    tmp_path: Path,
    script: list[str],
    stop: str,
) -> None:
    source = tmp_path / "source"
    original, _ = repository_pair(source)
    store = FileArtifactStore(tmp_path / "evidence")
    provider = SequenceProvider(store, [plan().model_dump_json(), proposal().model_dump_json()])
    backend = FeedbackBackend(store, script)
    result = AIInvestigator(provider, ReproductionExecutor(backend, store), store).run(
        context().issue,
        source,
        context().oracle,
        environment(),
        ref=original,
    )
    assert result.stop_reason == stop and len(provider.requests) == 2
    assert len(result.attempts) == 1


def test_duplicate_ast_is_not_reexecuted(tmp_path: Path) -> None:
    source = tmp_path / "source"
    original, _ = repository_pair(source)
    store = FileArtifactStore(tmp_path / "evidence")
    same = proposal().model_copy(update={"test_code": "# cosmetic change\n" + CODE})
    provider = SequenceProvider(
        store, [plan().model_dump_json(), proposal().model_dump_json(), same.model_dump_json()]
    )
    backend = FeedbackBackend(store, ["pass", "pass"])
    result = AIInvestigator(provider, ReproductionExecutor(backend, store), store).run(
        context().issue,
        source,
        context().oracle,
        environment(),
        ref=original,
    )
    assert result.stop_reason == "duplicate_candidate" and result.model_calls == 3
    assert len(backend.calls) == 2 and result.attempts[-1].result is None


def test_candidate_budget_counts_validation_rejections(tmp_path: Path) -> None:
    source = tmp_path / "source"
    original, _ = repository_pair(source)
    store = FileArtifactStore(tmp_path / "evidence")
    provider = SequenceProvider(store, [plan().model_dump_json(), "invalid", "invalid"])
    backend = FeedbackBackend(store, [])
    result = AIInvestigator(provider, ReproductionExecutor(backend, store), store).run(
        context().issue,
        source,
        context().oracle,
        environment(),
        ref=original,
        max_candidates=2,
    )
    assert result.stop_reason == "budget_exhausted" and result.model_calls == 3
    assert len(result.attempts) == 2 and not backend.calls


def test_model_error_stops_without_automatic_api_retries(tmp_path: Path) -> None:
    source = tmp_path / "source"
    original, _ = repository_pair(source)
    store = FileArtifactStore(tmp_path / "evidence")
    provider = SequenceProvider(store, [plan().model_dump_json(), "provider_error"])
    result = AIInvestigator(
        provider, ReproductionExecutor(FeedbackBackend(store, []), store), store
    ).run(
        context().issue,
        source,
        context().oracle,
        environment(),
        ref=original,
    )
    assert result.stop_reason == "model_error" and result.model_calls == 2
    assert result.attempts[0].generation_record is not None


@pytest.mark.parametrize(
    "update",
    [
        {"source_paths": ("../outside.py",)},
        {"source_paths": ("missing.py",)},
        {"source_paths": ("tests/test_calculator.py",)},
        {"test_paths": ("calculator.py",)},
        {"source_paths": ("calculator.py", "calculator.py")},
        {"evidence": ({"source": "issue.body", "quote": "fabricated"},)},
    ],
)
def test_model_file_selection_and_evidence_cannot_bypass_local_checks(
    tmp_path: Path,
    update: dict[str, object],
) -> None:
    source = tmp_path / "source"
    original, _ = repository_pair(source)
    store = FileArtifactStore(tmp_path / "evidence")
    bad = plan().model_dump(mode="json")
    bad.update(update)
    provider = SequenceProvider(store, [json.dumps(bad)])
    with pytest.raises((ValidationError, PolicyViolation)):
        AIPlanner(store, provider).plan(
            context().issue,
            source,
            context().oracle,
            ref=original,
            max_attempts=1,
        )
    assert any(
        b'"status": "rejected"' in item.read_bytes() for item in (store.root / "objects").iterdir()
    )


@pytest.mark.parametrize("budget", [0, 6])
def test_invalid_budget_is_rejected_before_a_model_request(tmp_path: Path, budget: int) -> None:
    store = FileArtifactStore(tmp_path / "evidence")
    provider = SequenceProvider(store, [])
    with pytest.raises(InputError):
        AIInvestigator(
            provider, ReproductionExecutor(FeedbackBackend(store, []), store), store
        ).run(
            context().issue,
            tmp_path,
            context().oracle,
            environment(),
            max_candidates=budget,
        )
    assert not provider.requests


def test_equal_commits_are_rejected_before_a_model_request(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _, fixed = repository_pair(source)
    store = FileArtifactStore(tmp_path / "evidence")
    provider = SequenceProvider(store, [])
    with pytest.raises(InputError, match="must differ"):
        AIInvestigator(
            provider, ReproductionExecutor(FeedbackBackend(store, []), store), store
        ).run(
            context().issue,
            source,
            context().oracle,
            environment(),
            ref=fixed,
            fixed_commit="HEAD",
        )
    assert not provider.requests


def test_ai_can_select_a_relevant_file_outside_the_seed_sources(tmp_path: Path) -> None:
    source = tmp_path / "source"
    repository_pair(source)
    with Repo(source) as repo:
        for number in range(5):
            noise = source / f"a{number}_decoy.py"
            noise.write_text("def identity(value):\n return value\n", encoding="utf-8")
            repo.index.add([noise.name])
        (source / "unicode_names.py").write_text(
            "def normalize(value):\n return value.lower()\n", encoding="utf-8"
        )
        repo.index.add(["unicode_names.py"])
        commit = repo.index.commit("extra source", skip_hooks=True).hexsha
    store = FileArtifactStore(tmp_path / "evidence")
    selected = plan().model_copy(update={"source_paths": ("unicode_names.py",), "test_paths": ()})
    provider = SequenceProvider(store, [selected.model_dump_json()])
    result = AIPlanner(store, provider).plan(context().issue, source, context().oracle, ref=commit)
    request = provider.requests[0]["user"]
    assert isinstance(request, dict)
    assert "unicode_names.py" not in {
        item["path"] for item in request["context"]["relevant_source_files"]
    }
    assert [item.path for item in result.context.relevant_source_files] == ["unicode_names.py"]
    assert result.context.existing_tests == ()


def test_uncertain_cleanup_aborts_without_another_candidate(tmp_path: Path) -> None:
    source = tmp_path / "source"
    original, _ = repository_pair(source)
    store = FileArtifactStore(tmp_path / "evidence")
    provider = SequenceProvider(store, [plan().model_dump_json(), proposal().model_dump_json()])

    class UncertainBackend:
        def run_test(self, request: PytestRunRequest) -> ExecutionResult:
            raise ExecutionBackendError("cleanup unknown", execution(), stage="cleanup")

    with pytest.raises(ExecutionBackendError):
        AIInvestigator(provider, ReproductionExecutor(UncertainBackend(), store), store).run(
            context().issue,
            source,
            context().oracle,
            environment(),
            ref=original,
        )
    assert len(provider.requests) == 2


def test_planning_error_is_repaired_within_its_own_budget(tmp_path: Path) -> None:
    source = tmp_path / "source"
    original, _ = repository_pair(source)
    store = FileArtifactStore(tmp_path / "evidence")
    bad = plan().model_dump(mode="json")
    bad["source_paths"] = ["tests/test_calculator.py"]
    provider = SequenceProvider(
        store, [json.dumps(bad), plan().model_dump_json(), proposal().model_dump_json()]
    )
    result = AIInvestigator(
        provider, ReproductionExecutor(FeedbackBackend(store, ["target", "target"]), store), store
    ).run(
        context().issue,
        source,
        context().oracle,
        environment(),
        ref=original,
    )
    assert result.stop_reason == "reproduced" and result.model_calls == 3
    request = provider.requests[1]["user"]
    assert isinstance(request, dict) and "invalid source_paths" in request["validation_error"]
    record = json.loads(store.read_bytes(result.plan_record))
    assert record["attempt"] == 2 and record["previous_record"] is not None


def test_planning_http_failure_does_not_trigger_another_request(tmp_path: Path) -> None:
    source = tmp_path / "source"
    original, _ = repository_pair(source)
    store = FileArtifactStore(tmp_path / "evidence")
    provider = SequenceProvider(store, ["provider_error"])
    with pytest.raises(ProviderError):
        AIPlanner(store, provider).plan(context().issue, source, context().oracle, ref=original)
    assert len(provider.requests) == 1


def test_excluded_source_paths_are_not_read_or_listed_for_the_model(tmp_path: Path) -> None:
    source = tmp_path / "source"
    repository_pair(source)
    with Repo(source) as repo:
        path = source / ".ssh/credential.py"
        path.parent.mkdir()
        path.write_text("never_send_private_material = True\n", encoding="utf-8")
        repo.index.add([".ssh/credential.py"])
        commit = repo.index.commit("excluded fixture", skip_hooks=True).hexsha
    store = FileArtifactStore(tmp_path / "evidence")
    provider = SequenceProvider(store, [plan().model_dump_json()])
    AIPlanner(store, provider).plan(context().issue, source, context().oracle, ref=commit)
    request = json.dumps(provider.requests)
    assert ".ssh/credential.py" not in request and "never_send_private_material" not in request


def test_cli_runs_investigation_and_writes_a_capsule_compatible_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    original, fixed = repository_pair(source)
    store = FileArtifactStore(tmp_path / "evidence")
    provider = SequenceProvider(store, [plan().model_dump_json(), proposal().model_dump_json()])
    backend = FeedbackBackend(
        store, ["pass", "pass", "target", "target", "target", "target", "pass", "pass"]
    )
    monkeypatch.setattr(commands, "OpenAICompatibleProvider", lambda config, artifacts: provider)
    monkeypatch.setattr(commands, "DockerSandboxRunner", lambda **kwargs: backend)
    issue = context().issue
    (tmp_path / "issue.json").write_text(
        json.dumps(
            {
                "html_url": issue.issue_url,
                "number": issue.number,
                "title": issue.title,
                "body": issue.body,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "oracle.json").write_text(context().oracle.model_dump_json(), encoding="utf-8")
    (tmp_path / "environment.json").write_text(environment().model_dump_json(), encoding="utf-8")
    (tmp_path / "seed.py").write_text(SEED, encoding="utf-8")
    result = CliRunner().invoke(
        app,
        [
            "investigate",
            str(source),
            str(tmp_path / "issue.json"),
            str(tmp_path / "oracle.json"),
            str(tmp_path / "environment.json"),
            "--model",
            "fixture",
            "--ref",
            original,
            "--fixed-commit",
            fixed,
            "--initial-test",
            str(tmp_path / "seed.py"),
            "--output",
            str(tmp_path / "session.json"),
            "--result-output",
            str(tmp_path / "result.json"),
            "--artifacts",
            str(store.root),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads((tmp_path / "session.json").read_bytes())["model_calls"] == 2
    assert json.loads((tmp_path / "result.json").read_bytes())["status"] == "DIFFERENTIAL_VALIDATED"


def test_cli_rejects_budget_before_model_or_file_access(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "investigate",
            str(tmp_path),
            "missing.json",
            "missing.json",
            "missing.json",
            "--model",
            "fixture",
            "--max-candidates",
            "0",
        ],
    )
    assert result.exit_code == 3 and "--max-candidates" in result.output


def test_feedback_is_redacted_before_clipping_and_omits_partial_private_keys(
    tmp_path: Path,
) -> None:
    from tests.test_capsule_io import captured

    store = FileArtifactStore(tmp_path / "evidence")
    provider = SequenceProvider(store, [])
    investigator = AIInvestigator(
        provider, ReproductionExecutor(FeedbackBackend(store, []), store), store
    )
    raw = "😀" * 3997 + "private-provider-key" + "tail"
    feedback = investigator._feedback(1, captured(store, log=raw))
    assert "pri" not in feedback.observations[0].stdout
    assert len(feedback.observations[0].stdout) <= 4000
    assert (
        investigator._clean("-----BEGIN PRIVATE KEY-----\npartial-key", 4000)
        == "[sensitive feedback omitted]"
    )
