from __future__ import annotations


def retry_delays(base: float, cap: float, attempts: int) -> list[float]:
    """Return capped exponential retry delays."""
    if attempts <= 0:
        return []
    return [min(cap, base * 2**index) for index in range(attempts)]
