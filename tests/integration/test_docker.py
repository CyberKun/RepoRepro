"""Opt-in real Docker security checks using only self-owned fixture code."""

import json
import os
import re
import zipfile
from pathlib import Path

import pytest
from git import Repo

from repo_repro.core.models import CommandSpec, SandboxPolicy, Termination
from repo_repro.sandbox.docker_runner import DockerCLI, DockerSandboxRunner
from repo_repro.storage.artifacts import FileArtifactStore

pytestmark = pytest.mark.docker_integration


@pytest.fixture
def backend(tmp_path: Path) -> DockerSandboxRunner:
    image = os.environ.get("REPRO_DOCKER_TEST_IMAGE")
    if not image:
        if os.environ.get("REPRO_REQUIRE_DOCKER") == "1":
            pytest.fail("REPRO_DOCKER_TEST_IMAGE must be an immutable, locally present image")
        pytest.skip("set REPRO_DOCKER_TEST_IMAGE to explicitly enable real Docker checks")
    return DockerSandboxRunner(image=image, artifacts=FileArtifactStore(tmp_path / "evidence"))


def commit_fixture(path: Path, content: str = "print('owned fixture')\n") -> Path:
    path.mkdir()
    (path / "probe.py").write_text(content, encoding="utf-8", newline="\n")
    (path / ".env").write_text("TOKEN=not-for-container", encoding="utf-8")
    with Repo.init(path, initial_branch="main") as repo:
        repo.index.add(["probe.py", ".env"])
        repo.index.commit("self-owned Docker fixture", skip_hooks=True)
    return path


def limits(**changes: object) -> SandboxPolicy:
    return SandboxPolicy.model_validate(
        {
            "isolation": "reviewed_docker",
            "memory_bytes": 268_435_456,
            "workspace_bytes_limit": 67_108_864,
            "timeout_seconds": 15,
            "pids": 32,
        }
        | changes
    )


def assert_removed(backend: DockerSandboxRunner, result_snapshot: bytes) -> None:
    metadata = json.loads(result_snapshot)
    assert metadata["cleanup_confirmed"] is True
    response = DockerCLI().run(["inspect", metadata["container_name"]])
    assert response.returncode != 0
    assert re.search(rb"no such (?:object|container)(?::|\s|$)", response.stderr, re.I)


def container_host_paths(path: Path) -> tuple[str, ...]:
    """Name a host fixture in Linux and Docker Desktop host-mount conventions."""
    if os.name != "nt":
        return (str(path),)
    drive = path.drive.removesuffix(":").lower()
    relative = "/".join(path.parts[1:])
    return tuple(
        f"{prefix}/{drive}/{relative}" for prefix in ("/run/desktop/mnt/host", "/host_mnt", "/mnt")
    )


def test_real_identity_network_limits_and_sensitive_paths(
    tmp_path: Path,
    backend: DockerSandboxRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPRO_HOST_SECRET", "must-not-forward")
    outside = tmp_path / "host-secret"
    outside.write_text("private", encoding="utf-8")
    fake_ssh_key = tmp_path / "host-home" / ".ssh" / "id_rsa"
    fake_ssh_key.parent.mkdir(parents=True)
    fake_ssh_key.write_text("self-owned SSH isolation fixture", encoding="utf-8")
    protected_paths = container_host_paths(outside) + container_host_paths(fake_ssh_key)
    source = commit_fixture(
        tmp_path / "source",
        f"""
import json, os, pathlib, socket
assert os.geteuid() == 10001
assert 'REPRO_HOST_SECRET' not in os.environ
for host_path in {protected_paths!r}:
    try:
        pathlib.Path(host_path).read_bytes()
    except OSError:
        pass
    else:
        raise AssertionError('host fixture was readable: ' + host_path)
assert not pathlib.Path('/workspace/.env').exists()
assert not pathlib.Path('/workspace/.git').exists()
assert not pathlib.Path('/var/run/docker.sock').exists()
try:
    pathlib.Path('/root/.ssh/id_rsa').read_bytes()
except OSError:
    pass
else:
    raise AssertionError('host SSH material was readable')
assert os.environ['HOME'] == '/tmp/repro-home'
assert set(os.listdir('/sys/class/net')) == {{'lo'}}
try:
    pathlib.Path('/etc/repro-write-probe').write_text('forbidden')
except OSError:
    pass
else:
    raise AssertionError('root filesystem was writable')
try:
    socket.create_connection(('169.254.169.254', 80), timeout=0.2)
except OSError:
    pass
else:
    raise AssertionError('metadata endpoint was reachable')
print(json.dumps({{'identity': os.geteuid(), 'isolated': True}}))
""",
    )
    result = backend.run(source, CommandSpec(argv=("python", "probe.py")), resource_limits=limits())
    stdout, stderr = backend.read_output(result)
    assert result.exit_code == 0, stderr
    assert json.loads(stdout)["isolated"] is True
    assert result.resource_usage is not None and result.resource_usage.cgroup_verified
    assert_removed(backend, backend.artifacts.read_bytes(result.backend_snapshot))


def test_real_nonzero_exit_is_not_a_start_failure(
    tmp_path: Path, backend: DockerSandboxRunner
) -> None:
    source = commit_fixture(tmp_path / "source", "raise SystemExit(17)\n")
    result = backend.run(source, CommandSpec(argv=("python", "probe.py")), resource_limits=limits())
    assert result.exit_code == 17
    assert result.termination is Termination.EXITED
    assert_removed(backend, backend.artifacts.read_bytes(result.backend_snapshot))


def test_real_timeout_terminates_descendants_and_removes_container(
    tmp_path: Path,
    backend: DockerSandboxRunner,
) -> None:
    source = commit_fixture(
        tmp_path / "source",
        """
import subprocess, sys, time
subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
time.sleep(60)
""",
    )
    result = backend.run(
        source,
        CommandSpec(argv=("python", "probe.py")),
        resource_limits=limits(timeout_seconds=2),
    )
    assert result.timed_out
    assert_removed(backend, backend.artifacts.read_bytes(result.backend_snapshot))


def test_real_pid_limit_stops_bounded_process_growth(
    tmp_path: Path, backend: DockerSandboxRunner
) -> None:
    source = commit_fixture(
        tmp_path / "source",
        """
import errno, json, subprocess, sys
children = []
limited = False
try:
    for _ in range(24):
        try:
            children.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']))
        except OSError as error:
            assert error.errno == errno.EAGAIN, error
            limited = True
            break
finally:
    for process in children:
        process.kill()
    for process in children:
        process.wait()
assert limited, len(children)
print(json.dumps({'pid_limit_observed': True}))
""",
    )
    result = backend.run(
        source, CommandSpec(argv=("python", "probe.py")), resource_limits=limits(pids=16)
    )
    stdout, stderr = backend.read_output(result)
    assert result.exit_code == 0, stderr
    assert json.loads(stdout)["pid_limit_observed"]
    assert_removed(backend, backend.artifacts.read_bytes(result.backend_snapshot))


def test_real_output_budget_cannot_grow_without_bound(
    tmp_path: Path, backend: DockerSandboxRunner
) -> None:
    source = commit_fixture(tmp_path / "source", "import sys\nsys.stdout.write('x' * 10000000)\n")
    result = backend.run(
        source,
        CommandSpec(argv=("python", "probe.py")),
        resource_limits=limits(output_bytes_limit=2048),
    )
    assert result.termination is Termination.OUTPUT_LIMIT
    assert result.stdout.size_bytes + result.stderr.size_bytes <= 2048
    assert_removed(backend, backend.artifacts.read_bytes(result.backend_snapshot))


def test_real_memory_limit_records_oom_and_removes_container(
    tmp_path: Path, backend: DockerSandboxRunner
) -> None:
    source = commit_fixture(
        tmp_path / "source",
        """
import time
allocated = []
while True:
    allocated.append(bytearray(16 * 1024 * 1024))
    time.sleep(0.01)
""",
    )
    result = backend.run(
        source,
        CommandSpec(argv=("python", "probe.py")),
        resource_limits=limits(memory_bytes=134_217_728),
    )
    assert result.termination is Termination.OOM, backend.read_output(result)
    assert result.exit_code == 137
    assert result.resource_usage is not None and result.resource_usage.oom_killed
    assert_removed(backend, backend.artifacts.read_bytes(result.backend_snapshot))


def test_real_offline_wheel_install_is_a_separate_experiment(
    tmp_path: Path,
    backend: DockerSandboxRunner,
) -> None:
    source = commit_fixture(tmp_path / "source")
    wheel = source / "owned_pkg-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("owned_pkg/__init__.py", "VALUE = 7\n")
        archive.writestr(
            "owned_pkg-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: owned-pkg\nVersion: 1.0\n",
        )
        archive.writestr(
            "owned_pkg-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr("owned_pkg-1.0.dist-info/RECORD", "")
    with Repo(source) as repo:
        repo.index.add([wheel.name])
        repo.index.commit("owned offline wheel", skip_hooks=True)
    result = backend.run_install(
        source,
        CommandSpec(
            argv=(
                "python",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--no-index",
                "--no-deps",
                "--target",
                "/workspace/site",
                f"/workspace/{wheel.name}",
            )
        ),
        resource_limits=limits(),
    )
    stdout, stderr = backend.read_output(result)
    assert result.exit_code == 0, stderr
    assert "Successfully installed" in stdout
    assert result.phase == "install"
    assert_removed(backend, backend.artifacts.read_bytes(result.backend_snapshot))
