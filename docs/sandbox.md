# Docker sandbox

The current backend requires a reviewed, locally cached Linux amd64 image and a Linux daemon with cgroup v2. It accepts an immutable image ID or digest and never automatically pulls an image, builds a target Dockerfile, or mounts a host repository.

## Execution

`DockerSandboxRunner.run()` takes a `CommandSpec` and `SandboxPolicy`. `run_install()` records a separate install phase. `run_test()` runs the fixed pytest launcher and collector against a pinned source snapshot. Each attempt uses a new container.

Source snapshots read committed regular Git blobs. Links, submodules, reserved generated-test paths, oversized trees, and sensitive host-directory inputs are rejected. Common credential/config/cache paths are excluded. A dirty working tree is not the execution source.

Containers use UID/GID 10001, no network, no host mounts, a read-only root filesystem, private namespaces, dropped capabilities, and no-new-privileges. Writable workspaces use bounded tmpfs. CPU, memory/swap, process count, workspace, output, and elapsed-time limits are requested from Docker and checked through inspect and cgroup/proc evidence.

The image is still a trusted input. The backend checks runtime identity and limits, but cannot determine that arbitrary image content is safe. Review it before selecting `reviewed_docker`; that declaration is not an external security audit.

## Evidence and failure

`ExecutionResult` records the pinned source, command, termination, exit code, stdout/stderr artifact references, resource measurements where available, backend snapshot, and cleanup status. `read_output()` reads captured text. `timed_out` derives from the observed termination reason.

Timeout, cancellation, excessive output, OOM, creation failures, and cleanup failures remain separate facts. Cleanup is checked against container ownership labels. If removal cannot be confirmed, an exception carries the attempted result and the caller must stop.

Installation and test containers do not share temporary files. Prepare dependencies in an approved immutable image before execution. General dependency resolution and environment freezing are not implemented.

## Running an owned fixture

Use the [image and demo instructions](../examples/README.md), then:

```bash
python examples/run_sandbox.py --repository .repo-repro/demo/repository \
  --image "$IMAGE_ID" --isolation reviewed_docker \
  --timeout 15 --command python -c "print('owned fixture')"
```

`IMAGE_ID` must identify the reviewed, locally cached image. Third-party code must not be executed on the host to prepare or test a fixture.

Ordinary Docker does not protect against every kernel/daemon attack or authenticate a malicious program's reports. For high-risk repositories, run the toolkit in a disposable VM with acquisition quotas. See [SECURITY.md](../SECURITY.md).
