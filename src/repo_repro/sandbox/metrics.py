"""Parse external Docker stats samples without claiming exact peaks or CPU totals."""

import re


def memory_bytes(value: str) -> int:
    match = re.fullmatch(r"([0-9.]+)\s*([KMGT]?i?B)", value.strip())
    if match is None:
        raise ValueError("invalid Docker memory sample")
    number, unit = match.groups()
    exponent = {"B": 0, "KB": 1, "MB": 2, "GB": 3, "TB": 4, "KiB": 1, "MiB": 2, "GiB": 3, "TiB": 4}[
        unit
    ]
    return int(float(number) * (1024 if "i" in unit else 1000) ** exponent)


def stats_sample(payload: object) -> tuple[int, float, int]:
    if not isinstance(payload, dict):
        raise ValueError("invalid Docker stats JSON")
    memory = memory_bytes(str(payload.get("MemUsage", "")).split(" / ")[0])
    cpu = float(str(payload.get("CPUPerc", "")).removesuffix("%"))
    pids = int(str(payload.get("PIDs", "")))
    if not 0 <= cpu <= 100_000 or pids < 0:
        raise ValueError("invalid Docker stats values")
    return memory, cpu, pids
