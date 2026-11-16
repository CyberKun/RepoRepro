"""Bounded Docker-client pipe capture with external container cancellation hooks."""

import contextlib
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import BinaryIO


@dataclass(frozen=True)
class ProcessOutput:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_limited: bool = False
    cancelled: bool = False


def capture_process(
    arguments: Sequence[str],
    *,
    payload: bytes = b"",
    timeout: float,
    output_limit: int,
    before_input: Callable[[], None] | None = None,
    on_abort: Callable[[], None] | None = None,
) -> ProcessOutput:
    process = subprocess.Popen(
        list(arguments),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    output = [bytearray(), bytearray()]
    lock = threading.Lock()
    overflow = threading.Event()
    size = 0

    def drain(stream: BinaryIO, index: int) -> None:
        nonlocal size
        while chunk := stream.read(65_536):
            with lock:
                retained = max(0, output_limit - size)
                output[index].extend(chunk[:retained])
                size += len(chunk)
                if size > output_limit:
                    overflow.set()

    def feed() -> None:
        assert process.stdin is not None
        try:
            remaining = memoryview(payload)
            while remaining:
                written = process.stdin.write(remaining)
                if written is None or written <= 0:
                    raise BrokenPipeError("Docker stdin closed during snapshot transfer")
                remaining = remaining[written:]
        except (BrokenPipeError, OSError):
            pass
        finally:
            process.stdin.close()

    readers = [
        threading.Thread(target=drain, args=(process.stdout, 0), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, 1), daemon=True),
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    cancelled = False
    try:
        if before_input is not None:
            before_input()
        writer = threading.Thread(target=feed, daemon=True)
        writer.start()
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if overflow.is_set() or time.monotonic() >= deadline:
                timed_out = not overflow.is_set()
                break
            time.sleep(0.02)
    except KeyboardInterrupt:
        cancelled = True
    finally:
        if process.poll() is None:
            try:
                if on_abort is not None:
                    on_abort()
            finally:
                if process.poll() is None:
                    process.kill()
        process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            with contextlib.suppress(OSError):
                stream.close()
    return ProcessOutput(
        process.returncode,
        bytes(output[0]),
        bytes(output[1]),
        timed_out,
        overflow.is_set(),
        cancelled,
    )
