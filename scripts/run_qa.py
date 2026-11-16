"""Run complete QA with explicit local Docker images and retain machine-readable evidence."""

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    reports = root / "reports"
    reports.mkdir(exist_ok=True)
    missing = [
        key for key in ("REPRO_DOCKER_TEST_IMAGE", "REPRO_PYTEST_IMAGE") if not os.environ.get(key)
    ]
    if missing:
        print("Full QA requires approved, locally cached images: " + ", ".join(missing))
        return 2
    environment = os.environ | {
        "REPRO_REQUIRE_DOCKER": "1",
        "REPRO_REQUIRE_ENGINE": "1",
        "PYTHONUTF8": "1",
    }
    checks = {
        "tests": [
            "pytest",
            "-q",
            "--junitxml=reports/tests.xml",
            "--cov=repo_repro",
            "--cov-branch",
            "--cov-report=html:reports/coverage",
            "--cov-report=xml:reports/coverage.xml",
            "--cov-report=json:reports/coverage.json",
            "--cov-report=term-missing",
        ],
        "ruff": ["ruff", "check", "."],
        "format": ["ruff", "format", "--check", "."],
        "mypy": ["mypy"],
        "pip-check": ["pip", "check"],
        "bandit": ["bandit", "-r", "src", "-f", "json", "-o", "reports/bandit.json"],
        "pip-audit": [
            "pip_audit",
            "--format",
            "json",
            "--output",
            "reports/pip-audit.json",
            "--progress-spinner",
            "off",
        ],
        "build": ["build", "--outdir", "reports/dist"],
    }
    results: dict[str, object] = {}
    failed = False
    for name, arguments in checks.items():
        print(f"Running {name}...", flush=True)
        started = datetime.now(UTC)
        with (reports / f"{name}.log").open("w", encoding="utf-8") as output:
            result = subprocess.run(
                [sys.executable, "-m", *arguments],
                cwd=root,
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=900,
            )
        results[name] = {
            "command": [sys.executable, "-m", *arguments],
            "exit_code": result.returncode,
            "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
        }
        failed |= result.returncode != 0
        if name == "tests" and (reports / "tests.xml").is_file():
            skipped = sum(
                int(suite.get("skipped", "0"))
                for suite in ET.parse(reports / "tests.xml").getroot().iter("testsuite")
            )
            if skipped:
                print(f"Full QA is incomplete: {skipped} tests were skipped", flush=True)
                failed = True
        (reports / "checks.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"{name}: exit {result.returncode}", flush=True)
    # Bandit deliberately retains all findings; its nonzero status needs human triage.
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
