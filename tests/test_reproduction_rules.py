"""Safety and verdicts cannot be replaced by a model's confidence or ordinary exit code."""

import pytest

from repo_repro.core.exceptions import PolicyViolation
from repo_repro.core.models import Termination
from repo_repro.reproduction.models import ReproductionStatus
from repo_repro.reproduction.safety import check_test_code, validate_proposal
from repo_repro.verification.verifier import assess_execution
from tests.repro_support import CODE, context, execution, proposal, report


def test_evidence_linked_proposal_passes_without_host_execution() -> None:
    validate_proposal(proposal(), context())


@pytest.mark.parametrize(
    "code",
    [
        "import os\ndef test_repro():\n assert os.system('echo bad') == 0",
        "def test_repro():\n exec('anything')\n assert 1 == 1",
        "def test_repro():\n assert False",
        "def test_repro():\n raise ValueError('target')",
        "print('module side effect')\n" + CODE,
        "import pytest\n@pytest.mark.xfail\ndef test_repro():\n assert False",
        "def test_repro():\n while True:\n  pass\n assert 1 == 1",
        "from calculator import *\ndef test_repro():\n assert 1 == 1",
        "def test_repro(tmp_path):\n assert 1 == 1",
    ],
)
def test_generated_unsafe_or_unverifiable_code_is_rejected(code: str) -> None:
    with pytest.raises(PolicyViolation):
        check_test_code(code)


def test_fabricated_evidence_and_wrong_source_calls_are_rejected() -> None:
    forged = proposal().model_copy(update={"test_code": "def test_repro():\n assert 2 + 2 == 5"})
    with pytest.raises(PolicyViolation):
        validate_proposal(forged, context())


def test_matching_call_failure_is_reproduced() -> None:
    outcome = assess_execution(execution(), context().oracle, report().model_dump_json().encode())
    assert outcome.status is ReproductionStatus.REPRODUCED


def test_wrong_failure_is_inconclusive() -> None:
    outcome = assess_execution(
        execution(),
        context().oracle,
        report(exception_type="ValueError", message="unrelated").model_dump_json().encode(),
    )
    assert outcome.status is ReproductionStatus.INCONCLUSIVE


def test_passed_call_is_not_reproduced() -> None:
    outcome = assess_execution(
        execution(0), context().oracle, report(passed=True).model_dump_json().encode()
    )
    assert outcome.status is ReproductionStatus.NOT_REPRODUCED


@pytest.mark.parametrize(
    ("termination", "expected"),
    [
        (Termination.TIMEOUT, ReproductionStatus.TIMEOUT),
        (Termination.OOM, ReproductionStatus.ENVIRONMENT_ERROR),
        (Termination.INFRA_ERROR, ReproductionStatus.ENVIRONMENT_ERROR),
        (Termination.OUTPUT_LIMIT, ReproductionStatus.INCONCLUSIVE),
    ],
)
def test_infrastructure_facts_take_precedence(
    termination: Termination, expected: ReproductionStatus
) -> None:
    assert assess_execution(execution(137, termination), context().oracle, None).status is expected


@pytest.mark.parametrize(
    "change",
    [
        {"session_complete": False},
        {"collection_complete": False},
        {"collected": ()},
        {"exit_code": 0},
        {"phases": ()},
    ],
)
def test_incomplete_or_contradictory_reports_cannot_reproduce(change: dict[str, object]) -> None:
    evidence = report().model_copy(update=change)
    outcome = assess_execution(execution(), context().oracle, evidence.model_dump_json().encode())
    assert outcome.status is ReproductionStatus.INCONCLUSIVE


def test_fixture_failure_does_not_count_as_the_target_bug() -> None:
    evidence = report()
    bad_setup = evidence.phases[0].model_copy(update={"outcome": "failed"})
    evidence = evidence.model_copy(update={"phases": (bad_setup, *evidence.phases[1:])})
    assert (
        assess_execution(execution(), context().oracle, evidence.model_dump_json().encode()).status
        is ReproductionStatus.ENVIRONMENT_ERROR
    )


def test_skip_xfail_and_missing_report_are_not_success() -> None:
    evidence = report()
    skipped = evidence.phases[1].model_copy(update={"outcome": "skipped", "was_xfail": True})
    evidence = evidence.model_copy(
        update={"phases": (evidence.phases[0], skipped, evidence.phases[2])}
    )
    assert (
        assess_execution(execution(), context().oracle, evidence.model_dump_json().encode()).status
        is ReproductionStatus.INCONCLUSIVE
    )
    assert (
        assess_execution(execution(), context().oracle, None).status
        is ReproductionStatus.INCONCLUSIVE
    )


def test_truncated_failure_evidence_is_inconclusive() -> None:
    evidence = report()
    changed = evidence.phases[1].model_copy(update={"truncated": True})
    evidence = evidence.model_copy(
        update={"phases": (evidence.phases[0], changed, evidence.phases[2])}
    )
    assert (
        assess_execution(execution(), context().oracle, evidence.model_dump_json().encode()).status
        is ReproductionStatus.INCONCLUSIVE
    )


@pytest.mark.parametrize(
    "body",
    [
        "    parse_value('')\n    assert 1 == 0, 'returns zero'\n",
        "    result = parse_value('')\n    if result == -1:\n        assert result == 0\n",
        "    result = parse_value('')\n    result = 999\n    assert result == 0\n",
        "    return\n    assert parse_value('') == 0\n",
    ],
)
def test_discarded_results_and_conditional_assertions_are_refused(body: str) -> None:
    candidate = proposal().model_copy(
        update={"test_code": "from calculator import parse_value\ndef test_repro():\n" + body}
    )
    with pytest.raises(PolicyViolation):
        validate_proposal(candidate, context())
